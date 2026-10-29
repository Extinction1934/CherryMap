import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { nextMarkerPollDelay } from './bridgeDisplay';
import { nearbyLayerState } from './nearbyViewport';
import { workerViewportKey } from './workerViewport';
import { retainsGeographicFrame } from './geographicFrameState';
import type { Bootstrap, GeographicBounds, IndexSnapshot, MarkerFusion, MarkerSnapshot, MarkerWorkerStatus, NearbyState, Place, POISnapshot, ViewState } from './types';

function isStreetPin(place: Place) {
  if (place.category === 'live-cluster' || place.category === 'portx' || place.category === 'portx-cluster' || place.category === 'operator' || place.category === 'operator-cluster' || place.category === 'iplasma-c3' || place.category === 'iplasma-c3-cluster') return false;
  return (place.origin === 'open-poi' || place.origin === 'neural-feed') && !place.cluster;
}

function insideView(place: Place, bounds: GeographicBounds | undefined) {
  if (!bounds) return true;
  const padLat = Math.max((bounds.north - bounds.south) * 0.3, 0.004);
  const padLng = Math.max((bounds.east - bounds.west) * 0.3, 0.004);
  if (place.lat < bounds.south - padLat || place.lat > bounds.north + padLat) return false;
  const west = bounds.west - padLng;
  const east = bounds.east + padLng;
  if (west <= east) return place.lng >= west && place.lng <= east;
  return place.lng >= west || place.lng <= east;
}

// A refresh that comes back empty, or as bubbles, must not take the pins off
// the map. Close zoom keeps every street pin still in view and only adds.
function retainStreetPins(previous: Place[], incoming: Place[], zoom: number, bounds: GeographicBounds | undefined) {
  if (zoom < 13) return incoming;
  const prevStreet = previous.filter(place => isStreetPin(place) && insideView(place, bounds));
  if (!prevStreet.length) return incoming;
  const incomingIds = new Set(incoming.map(place => place.id));
  const dropped = prevStreet.filter(place => !incomingIds.has(place.id));
  const incomingStreet = incoming.some(isStreetPin);
  if (!dropped.length && incomingStreet && incoming.length >= prevStreet.length) return incoming;
  if (dropped.length < prevStreet.length * 0.2 && incomingStreet && incoming.length >= prevStreet.length) return incoming;
  const merged = new Map<string, Place>();
  for (const place of prevStreet) merged.set(place.id, place);
  for (const place of incoming) {
    if (place.cluster && !incomingStreet) continue;
    merged.set(place.id, place);
  }
  return [...merged.values()];
}

function placeSignature(places: Place[]) {
  return places.map(place => place.id).sort().join('|');
}

export default function useMarkerRuntime(bootstrap: Bootstrap | null, view: ViewState, nearbyVisible: boolean, indexVisible: boolean) {
  const [locations, setLocations] = useState<Place[] | null>(null);
  const [worker, setWorker] = useState<MarkerWorkerStatus | null>(null);
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'error'>('connecting');
  const [error, setError] = useState('');
  const [nearby, setNearby] = useState<NearbyState>({ status: 'zoom', places: [], hasMore: false });
  const [index, setIndex] = useState<IndexSnapshot | null>(null);
  const [fusion, setFusion] = useState<MarkerFusion | null>(null);
  const [transitioning, setTransitioning] = useState(false);
  const [poi, setPoi] = useState<POISnapshot | null>(null);
  const poiSignature = useRef('');
  const liveSignature = useRef('');
  const poiPlacesRef = useRef<Place[]>([]);
  const viewRef = useRef(view);
  viewRef.current = view;
  const [regionQuery, setRegionQuery] = useState('');
  const revision = useRef<number | null>(null);
  const signature = useRef('');
  const nearbySignature = useRef('');
  const indexSignature = useRef('');
  const frame = useRef<MarkerFusion | null>(null);
  const fusionSignature = useRef('');
  const targetKey = workerViewportKey(view, nearbyVisible, indexVisible);
  const targetRef = useRef(targetKey);
  targetRef.current = targetKey;

  useEffect(() => {
    if (!bootstrap) return;
    setLocations(bootstrap.locations);
    setWorker(bootstrap.worker || null);
    if (bootstrap.index) setIndex(bootstrap.index);
    if (bootstrap.poi) {
      setPoi(bootstrap.poi);
      poiPlacesRef.current = bootstrap.poi.places;
      poiSignature.current = placeSignature(bootstrap.poi.places);
    }
    if (bootstrap.fusion) {
      frame.current = bootstrap.fusion;
      fusionSignature.current = placeSignature(bootstrap.fusion.markers);
      setFusion(bootstrap.fusion);
    }
    revision.current = bootstrap.worker?.revision ?? null;
    signature.current = JSON.stringify(bootstrap.locations);
  }, [bootstrap]);

  useEffect(() => {
    if (!bootstrap) return;
    nearbySignature.current = ''; indexSignature.current = '';
    if (!targetKey) { setRegionQuery(''); setTransitioning(false); return; }
    setTransitioning(true);
    setNearby(previous => ({ ...previous, status: !nearbyVisible ? 'hidden' : view.zoom < 12 ? 'zoom' : 'loading' }));
    setIndex(previous => previous ? { ...previous, status: indexVisible ? 'loading' : 'hidden' } : previous);
    setPoi(previous => {
      if (!previous) return previous;
      if (view.zoom >= 13 && previous.places.some(isStreetPin)) return previous;
      return { ...previous, status: !indexVisible ? 'hidden' : view.zoom < 12 ? 'zoom' : 'loading' };
    });
    // Keep the exact last server-produced fused frame, not a client merge. Layer
    // switches still control visibility while the next area is being registered.
    const timer = setTimeout(() => setRegionQuery(targetKey), 220);
    return () => clearTimeout(timer);
  }, [bootstrap, targetKey, nearbyVisible, indexVisible]);

  useEffect(() => {
    if (!bootstrap) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const refresh = async () => {
      const started = performance.now();
      let failed = false;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 10000);
      try {
        const snapshot = await api<MarkerSnapshot>(`/api/markers${regionQuery}`, { signal: controller.signal });
        if (!active) return;
        if (!snapshot.worker || !Array.isArray(snapshot.locations)) throw new Error('The marker backend returned an invalid snapshot.');
        setWorker(snapshot.worker);
        const nextSignature = JSON.stringify(snapshot.locations);
        if (revision.current !== snapshot.worker.revision || signature.current !== nextSignature) {
          revision.current = snapshot.worker.revision; signature.current = nextSignature;
          setLocations(snapshot.locations);
        }
        if (targetRef.current === regionQuery) {
          const wantsIndex = regionQuery.includes('index=true') || (!regionQuery && indexVisible);
          const streetFeed = Array.isArray(snapshot.poi?.places) && snapshot.poi.places.some(place =>
            (place.origin === 'open-poi' || place.origin === 'neural-feed') && !place.cluster);
          const retaining = retainsGeographicFrame(snapshot, wantsIndex, frame.current) && !streetFeed;
          if (snapshot.worker.runner?.kind === 'cherrypro-source-extension') {
            if (!snapshot.fusion || !Array.isArray(snapshot.fusion.markers)) throw new Error('The CherryPro extension did not return its fused marker collection.');
            if (!retaining) {
              const nextFusionSignature = placeSignature(snapshot.fusion.markers);
              if (fusionSignature.current !== nextFusionSignature) {
                fusionSignature.current = nextFusionSignature;
                frame.current = snapshot.fusion;
                setFusion(snapshot.fusion);
              }
            } else if (snapshot.poi && Array.isArray(snapshot.poi.places) && snapshot.poi.places.length > 0 && frame.current) {
              // Keep the last ready index frame, but never freeze POI / NeuralLink pins out of it.
              const kept = frame.current.markers.filter(marker => marker.origin !== 'open-poi' && marker.origin !== 'neural-feed');
              const next = { ...frame.current, markers: [...kept, ...snapshot.poi.places] };
              frame.current = next;
              setFusion(next);
            }
          }
          if (regionQuery.includes('nearby=true')) {
            if (!snapshot.nearby || !Array.isArray(snapshot.nearby.places)) throw new Error('The nearby worker did not return a valid marker snapshot.');
            const nextNearbySignature = JSON.stringify(snapshot.nearby);
            if (nearbySignature.current !== nextNearbySignature) {
              nearbySignature.current = nextNearbySignature;
              setNearby(nearbyLayerState(snapshot.nearby));
            }
          }
          if (snapshot.index) {
            if (retaining) setIndex(previous => previous ? { ...previous, status: snapshot.index.status, message: snapshot.index.message } : snapshot.index);
            else {
              const nextIndexSignature = JSON.stringify(snapshot.index);
              if (indexSignature.current !== nextIndexSignature) { indexSignature.current = nextIndexSignature; setIndex(snapshot.index); }
            }
          } else if (wantsIndex) throw new Error('The geographic index worker did not return a valid snapshot.');
          if (snapshot.poi && Array.isArray(snapshot.poi.places)) {
            const current = viewRef.current;
            const kept = retainStreetPins(poiPlacesRef.current, snapshot.poi.places, current.zoom, current.bounds);
            const nextPoiSignature = placeSignature(kept);
            const liveStamp = kept.filter(place => place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'portx' || place.category === 'portx-cluster' || place.category === 'operator' || place.category === 'operator-cluster' || place.category === 'iplasma-c3' || place.category === 'iplasma-c3-cluster' || place.category === 'interconnect-fusion').map(place => `${place.id}:${place.refreshed_at || ''}:${place.observed_at || ''}`).join('|');
            // Same pins already on the map. Do not swap the array — that
            // removes and recreates every marker, which reads as a blink.
            if (poiSignature.current !== nextPoiSignature) {
              poiSignature.current = nextPoiSignature;
              liveSignature.current = liveStamp;
              poiPlacesRef.current = kept;
              setPoi({
                ...snapshot.poi,
                places: kept,
                status: kept.length ? 'ready' : snapshot.poi.status,
                loaded_count: kept.length,
                total_in_view: Math.max(snapshot.poi.total_in_view, kept.length),
              });
            } else if (liveStamp && liveStamp !== liveSignature.current) {
              liveSignature.current = liveStamp;
              const incoming = new Map(snapshot.poi.places.filter(place => place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'portx' || place.category === 'portx-cluster' || place.category === 'operator' || place.category === 'operator-cluster' || place.category === 'iplasma-c3' || place.category === 'iplasma-c3-cluster' || place.category === 'interconnect-fusion').map(place => [place.id, place]));
              const refreshed = kept.map(place => place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'portx' || place.category === 'portx-cluster' || place.category === 'operator' || place.category === 'operator-cluster' || place.category === 'iplasma-c3' || place.category === 'iplasma-c3-cluster' || place.category === 'interconnect-fusion' ? incoming.get(place.id) || place : place);
              poiPlacesRef.current = refreshed;
              setPoi(previous => previous ? { ...previous, places: refreshed } : previous);
            } else if (kept.length) {
              setPoi(previous => previous?.status === 'ready' ? previous : previous ? { ...previous, status: 'ready' } : previous);
            }
          }
          setTransitioning(retaining);
        }
        setConnection('connected'); setError('');
      } catch (cause) {
        failed = true;
        if (!active) return;
        const aborted = cause instanceof DOMException ? cause.name === 'AbortError' : cause instanceof Error && cause.name === 'AbortError';
        const unavailable = cause instanceof Error && /HTTP 503|unavailable|timed out/i.test(cause.message);
        const message = !aborted && cause instanceof Error ? cause.message : 'Marker refresh timed out. Retrying while keeping the last valid geographic frame.';
        setError(message);
        // A slow frame is not a dead bridge. Keep the chip attached and the last pins up.
        if (!aborted && !unavailable) setConnection('error');
        if (targetRef.current === regionQuery) {
          nearbySignature.current = ''; indexSignature.current = '';
          if (regionQuery.includes('nearby=true')) setNearby(previous => ({ ...previous, status: 'error', message }));
          if (regionQuery.includes('index=true')) { setTransitioning(true); setIndex(previous => previous ? { ...previous, status: 'error', message } : previous); }
        }
      } finally {
        clearTimeout(timeout);
        if (active) timer = setTimeout(refresh, nextMarkerPollDelay(performance.now() - started, failed));
      }
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, [bootstrap, regionQuery, indexVisible]);

  return { locations, worker, connection, error, nearby, index, poi, fusion, transitioning };
}
