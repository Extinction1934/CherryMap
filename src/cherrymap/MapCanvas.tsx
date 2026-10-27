import { useEffect, useRef, useState } from 'react';
import { AlertCircle, LoaderCircle, RefreshCw } from 'lucide-react';
import { clusterMarkerIcon, clusterMarkerLabel, geographicOverviewIcon, markerIcon, markerPopup } from './markerPopup';
import { geographicLabelIds } from './geographicLabelLayout';
import { geographicCountry } from './geographicPresentation';
import { poiCategory } from './poiPresentation';
import { normalizeLongitude, wrapWorldOffset } from './geographicCoordinates';
import { bridgeProviderPOIClick } from './providerPOIClick';
import { reconcileSourceMarkers, type MarkerRecord } from './sourceMarkerSync';
import type { Bootstrap, MapActions, MapStyle, Place, RouteData, ViewState } from './types';
import { ensureGoogleMapsEngine } from './customGoogleMapsEngine';
import { interconnectGraph } from './interconnect';

let mapsPromise: Promise<void> | null = null;
function loadMaps(_config: Bootstrap['maps']): Promise<void> {
  // Custom renderer from the CherryMap maps package — no Google Maps JavaScript SDK.
  ensureGoogleMapsEngine();
  if (window.google?.maps?.Map) return Promise.resolve();
  mapsPromise = null;
  return Promise.reject(new Error('The custom map renderer did not start.'));
}

interface Props {
  bootstrap: Bootstrap;
  locations: Place[];
  style: MapStyle;
  sourceVisible: boolean;
  nearbyVisible: boolean;
  nearbyPlaces: Place[];
  indexVisible: boolean;
  indexPlaces: Place[];
  poiPlaces: Place[];
  onProviderPlace: (id: string) => void;
  selected: Place | null;
  route: RouteData | null;
  onSelect: (place: Place) => void;
  onView: (view: ViewState) => void;
  onReady: (actions: MapActions) => void;
  onDirections: (place: Place) => void;
  onSave: (place: Place) => void;
  onExpand: (place: Place) => void;
  onDeselect: () => void;
}

export default function MapCanvas(props: Props) {
  const element = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const callbacks = useRef(props);
  callbacks.current = props;
  const sourceMarkers = useRef<MarkerRecord[]>([]);
  const nearbyMarkers = useRef<MarkerRecord[]>([]);
  const indexMarkers = useRef<MarkerRecord[]>([]);
  const poiMarkers = useRef<MarkerRecord[]>([]);
  const selectedMarker = useRef<google.maps.Marker | null>(null);
  const routeLine = useRef<google.maps.Polyline | null>(null);
  const interconnectLines = useRef<google.maps.Polyline[]>([]);
  const interconnectHandles = useRef<google.maps.Marker[]>([]);
  const popup = useRef<google.maps.InfoWindow | null>(null);
  const tooltip = useRef<google.maps.InfoWindow | null>(null);
  const openPlaceId = useRef<string | null>(null);
  const hoverPlaceId = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  const showPopup = (place: Place, marker: google.maps.Marker) => {
    if (!mapRef.current || !popup.current) return;
    tooltip.current?.close(); hoverPlaceId.current = null;
    popup.current.setContent(markerPopup(place, {
      directions: value => callbacks.current.onDirections(value),
      save: value => callbacks.current.onSave(value),
      expand: value => callbacks.current.onExpand(value),
    }));
    popup.current.setOptions({ ariaLabel: `Location details: ${place.name}`, disableAutoPan: openPlaceId.current === place.id });
    popup.current.open({ map: mapRef.current, anchor: marker, shouldFocus: false });
    openPlaceId.current = place.id;
  };

  const createMarker = (place: Place, map: google.maps.Map): google.maps.Marker => {
    const marker = new google.maps.Marker({
      map, position: { lat: place.lat, lng: place.lng },
      title: place.marker?.title || place.name,
      icon: place.cluster ? clusterMarkerIcon(place.cluster.count, false, place.marker?.color) : place.overview ? geographicOverviewIcon() : markerIcon(place.origin === 'source', false, place.marker?.color),
      clickable: true, optimized: false,
      zIndex: place.category === 'portx' || place.category === 'portx-cluster' ? 180 : place.category === 'operator' || place.category === 'operator-cluster' ? 175 : place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'live-cluster' ? 170 : place.category === 'restricted-lattice' || place.category === 'lattice-cluster' ? 160 : place.origin === 'neural-feed' ? 150 : place.origin === 'open-poi' && place.cluster ? 120 : place.origin === 'source' ? 100 : place.cluster ? 10 : 30,
    });
    const currentPlace = () => [...callbacks.current.locations, ...callbacks.current.indexPlaces, ...callbacks.current.poiPlaces, ...callbacks.current.nearbyPlaces].find(item => item.id === place.id) || place;
    marker.addListener('click', () => {
      const latest = currentPlace();
      callbacks.current.onSelect(latest);
      showPopup(latest, marker);
    });
    marker.addListener('mouseover', () => {
      if (openPlaceId.current === place.id) return;
      hoverPlaceId.current = place.id;
      refreshHover();
    });
    marker.addListener('mouseout', () => { hoverPlaceId.current = null; tooltip.current?.close(); });
    return marker;
  };

  const refreshHover = () => {
    const id = hoverPlaceId.current;
    const map = mapRef.current;
    if (!id || !map || !tooltip.current || id === openPlaceId.current) return;
    const record = [...sourceMarkers.current, ...indexMarkers.current, ...poiMarkers.current, ...nearbyMarkers.current].find(item => item.place.id === id);
    if (!record) { tooltip.current.close(); hoverPlaceId.current = null; return; }
    const latest = record.place;
    const content = document.createElement('div'); content.className = 'marker-hover-tooltip';
    const title = document.createElement('strong'); title.textContent = latest.marker?.title || latest.name;
    const detail = document.createElement('small');
    detail.textContent = latest.category === 'portx-cluster' ? 'PortX signal cluster · click to expand'
      : latest.category === 'portx' ? 'PortX terminal signal · click for the readout'
      : latest.category === 'operator-cluster' ? 'Operator cluster · click to expand'
      : latest.category === 'operator' ? 'Operator feed · TPM-119 bridge · click for the readout'
      : latest.category === 'anaconda-cluster' ? 'Anaconda intelligence cluster · click to expand'
      : latest.category === 'anaconda' || latest.category === 'live-intelligence' ? 'Anaconda intelligence · NeuralLink lattice · click for the intel readout'
      : latest.category === 'lattice-cluster' ? 'Restricted Lattice partner cluster · click to expand'
      : latest.cluster && latest.origin === 'neural-feed' ? 'NeuralLink fusion cluster · click to expand'
      : latest.cluster && latest.origin === 'open-poi' ? 'Software-bridge fusion cluster · click to expand'
      : latest.origin === 'source' ? 'Python source annotation · click for details'
      : latest.origin === 'open-poi' ? `${poiCategory(latest)} · open POI · click for details`
      : latest.category === 'restricted-lattice' ? 'NeuralLink Restricted Lattice Partner Direct · click for details'
      : latest.origin === 'neural-feed' ? 'NeuralLink internal feed · click for details'
      : latest.origin === 'open-index' ? `${geographicCountry(latest)} · indexed place · click for details`
      : latest.cluster ? 'Indexed coverage · click to expand'
      : `${poiCategory(latest)} · provider place · click for details`;
    content.append(title, detail);
    tooltip.current.setContent(content);
    tooltip.current.open({ map, anchor: record.marker, shouldFocus: false });
  };

  const refreshLabels = () => {
    const map = mapRef.current;
    const zoom = map?.getZoom() ?? 3;
    const projection = map?.getProjection();
    const center = map?.getCenter();
    const centerPoint = center && projection?.fromLatLngToPoint(new google.maps.LatLng(center.lat(), normalizeLongitude(center.lng())));
    const bounds = map?.getBounds();
    const scale = 2 ** zoom;
    const worldWidth = 256 * scale;
    const projectPlace = (place: Place) => {
      if (!projection || !centerPoint || !bounds?.contains({ lat: place.lat, lng: place.lng })) return null;
      const point = projection.fromLatLngToPoint(new google.maps.LatLng(place.lat, place.lng));
      if (!point) return null;
      const delta = (point.x - centerPoint.x) * scale;
      return { x: wrapWorldOffset(delta, worldWidth), y: (point.y - centerPoint.y) * scale };
    };
    const indexedLabels = geographicLabelIds(callbacks.current.indexPlaces, projectPlace, callbacks.current.selected?.id || null);
    [...sourceMarkers.current, ...indexMarkers.current, ...poiMarkers.current, ...nearbyMarkers.current].forEach(({ marker, place }) => {
      const selected = callbacks.current.selected?.id === place.id;
      const source = place.origin === 'source';
      const neural = place.origin === 'neural-feed';
      const lattice = place.category === 'restricted-lattice' || place.category === 'lattice-cluster';
      const portx = place.category === 'portx' || place.category === 'portx-cluster';
      const operator = place.category === 'operator' || place.category === 'operator-cluster';
      const live = place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'live-cluster';
      const poi = place.origin === 'open-poi' || neural;
      const bridgeCluster = !!place.cluster && place.origin === 'open-poi';
      const stop = poi && /platform|bus_stop|halt|station/.test(place.category || '');
      const indexed = place.origin === 'open-index' || place.origin === 'index-cluster';
      marker.setVisible((source || neural ? callbacks.current.sourceVisible : indexed || poi ? callbacks.current.indexVisible : callbacks.current.nearbyVisible) || selected);
      marker.setIcon(place.cluster ? clusterMarkerIcon(place.cluster.count, selected, place.marker?.color) : place.overview ? geographicOverviewIcon(selected) : markerIcon(source, selected, place.marker?.color));
      marker.setZIndex(selected ? 1000 : portx ? 180 : operator ? 175 : live ? 170 : lattice ? 160 : neural ? 150 : bridgeCluster ? 120 : source ? 100 : stop ? 36 : poi ? 70 : place.cluster ? 10 : 30);
      const showLabel = portx || operator || !!place.cluster || selected || (neural ? zoom >= 2 : source ? zoom >= 7 : poi ? zoom >= 12 : indexed ? indexedLabels.has(place.id) : zoom >= 12);
      marker.setLabel(showLabel ? {
        text: portx || operator || place.category === 'anaconda' || place.category === 'anaconda-cluster' ? (place.marker?.label || place.name) : place.cluster ? clusterMarkerLabel(place.cluster.count) : place.marker?.label || place.name,
        color: portx ? '#c45a12' : operator ? '#be185d' : live ? '#1a4fbf' : lattice ? '#1e6b48' : neural ? '#5c3d8f' : bridgeCluster ? '#1e6b48' : source ? '#92304a' : poi ? '#24312c' : indexed ? '#396b94' : '#356d63',
        fontFamily: 'Manrope, sans-serif', fontSize: '11px', fontWeight: '700',
        className: portx ? 'native-place-label portx' : operator ? 'native-place-label operator' : place.cluster ? 'native-cluster-label' : `native-place-label ${source ? 'source' : live ? 'live' : lattice ? 'lattice' : neural ? 'neural' : poi ? 'poi' : indexed ? 'indexed' : 'provider'}`,
      } : null);
    });
  };

  useEffect(() => {
    let active = true;
    const listeners: google.maps.MapsEventListener[] = [];
    window.gm_authFailure = () => setError('The map provider could not authorize this map. Source search remains available.');
    loadMaps(props.bootstrap.maps).then(() => {
      if (!active || !element.current) return;
      const map = new google.maps.Map(element.current, {
        center: { lat: 20, lng: 0 }, zoom: 2,
        mapTypeId: 'roadmap', disableDefaultUI: true,
        gestureHandling: 'greedy', clickableIcons: true,
        minZoom: 0, maxZoom: 20, backgroundColor: '#e5eae6',
        styles: [
          { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#cddfe4' }] },
          { featureType: 'landscape', elementType: 'geometry', stylers: [{ color: '#f1f0e8' }] },
          { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#ffffff' }] },
          { featureType: 'administrative', elementType: 'geometry.stroke', stylers: [{ color: '#c5cdc4' }] },
          { elementType: 'labels.text.fill', stylers: [{ color: '#6a746b' }] },
          { elementType: 'labels.text.stroke', stylers: [{ color: '#f8f8f3' }] },
        ],
      });
      mapRef.current = map;
      popup.current = new google.maps.InfoWindow({ maxWidth: 320, minWidth: 220 });
      tooltip.current = new google.maps.InfoWindow({ maxWidth: 260, disableAutoPan: true });
      listeners.push(popup.current.addListener('closeclick', () => {
        openPlaceId.current = null;
        callbacks.current.onDeselect();
      }));
      sourceMarkers.current = callbacks.current.locations.map(place => ({ place, marker: createMarker(place, map) }));
      const updateView = () => {
        const center = map.getCenter();
        const bounds = map.getBounds();
        if (!center) return;
        const zoom = map.getZoom() ?? 3;
        const northEast = bounds?.getNorthEast();
        const radius = northEast
          ? Math.round(Math.min(5000, Math.max(150, Math.abs(northEast.lat() - center.lat()) * 111320)))
          : 1500;
        callbacks.current.onView({
          lat: center.lat(), lng: normalizeLongitude(center.lng()), zoom, radius, bounds: bounds?.toJSON(),
          visibleIds: callbacks.current.locations.filter(place => bounds?.contains({ lat: place.lat, lng: place.lng })).map(place => place.id),
        });
        refreshLabels();
      };
      listeners.push(map.addListener('click', (event: google.maps.MapMouseEvent | google.maps.IconMouseEvent) => {
        bridgeProviderPOIClick(event, id => callbacks.current.onProviderPlace(id));
      }));
      listeners.push(map.addListener('idle', updateView));
      listeners.push(map.addListener('zoom_changed', refreshLabels));
      callbacks.current.onReady({
        flyTo: place => {
          if (place.cluster) { map.fitBounds(place.cluster.bounds, { top: 90, bottom: 90, left: innerWidth > 760 ? 420 : 30, right: 50 }); return; }
          map.panTo({ lat: place.lat, lng: place.lng });
          if ((map.getZoom() ?? 3) < 16) map.setZoom(16);
        },
        zoom: delta => map.setZoom((map.getZoom() ?? 3) + delta),
        tilt: delta => map.setTilt((map.getTilt() || 0) + delta),
        heading: delta => map.setHeading((map.getHeading() || 0) + delta),
        level: () => { map.setTilt(0); map.setHeading(0); },
        world: () => map.fitBounds({ south: -80, north: 85, west: -180, east: 180 }, { top: 85, bottom: 70, left: innerWidth > 760 ? 335 : 40, right: 35 }),
        fitSource: () => {
          if (!callbacks.current.locations.length) return;
          const bounds = new google.maps.LatLngBounds();
          callbacks.current.locations.forEach(place => bounds.extend({ lat: place.lat, lng: place.lng }));
          map.fitBounds(bounds, { top: 100, bottom: 100, left: innerWidth > 760 ? 420 : 40, right: 60 });
        },
      });
      map.fitBounds({ south: -80, north: 85, west: -180, east: 180 }, { top: 85, bottom: 70, left: innerWidth > 760 ? 335 : 40, right: 35 });
      setReady(true); setError('');
    }).catch(e => { if (active) setError(e.message); });
    return () => {
      active = false;
      listeners.forEach(listener => listener.remove());
      [...sourceMarkers.current, ...indexMarkers.current, ...poiMarkers.current, ...nearbyMarkers.current].forEach(({ marker }) => {
        google.maps.event.clearInstanceListeners(marker); marker.setMap(null);
      });
      sourceMarkers.current = []; indexMarkers.current = []; poiMarkers.current = []; nearbyMarkers.current = [];
      routeLine.current?.setMap(null); selectedMarker.current?.setMap(null);
      popup.current?.close(); tooltip.current?.close();
      openPlaceId.current = null; mapRef.current = null;
    };
  }, [props.bootstrap.maps, attempt]);

  useEffect(() => { mapRef.current?.setMapTypeId(props.style); }, [props.style, ready]);
  useEffect(() => { refreshLabels(); }, [props.sourceVisible, props.indexVisible, props.nearbyVisible, props.selected, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    sourceMarkers.current = reconcileSourceMarkers(sourceMarkers.current, props.locations,
      place => createMarker(place, map),
      ({ marker }) => { google.maps.event.clearInstanceListeners(marker); marker.setMap(null); });
    refreshLabels();
  }, [props.locations, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    nearbyMarkers.current = reconcileSourceMarkers(nearbyMarkers.current, props.nearbyPlaces,
      place => createMarker(place, map),
      ({ marker }) => { google.maps.event.clearInstanceListeners(marker); marker.setMap(null); });
    refreshLabels();
    refreshHover();
  }, [props.nearbyPlaces, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    indexMarkers.current = reconcileSourceMarkers(indexMarkers.current, props.indexPlaces,
      place => createMarker(place, map),
      ({ marker }) => { google.maps.event.clearInstanceListeners(marker); marker.setMap(null); });
    refreshLabels(); refreshHover();
  }, [props.indexPlaces, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    poiMarkers.current = reconcileSourceMarkers(poiMarkers.current, props.poiPlaces.filter(place => place.category !== 'interconnect-fusion'),
      place => createMarker(place, map),
      ({ marker }) => { google.maps.event.clearInstanceListeners(marker); marker.setMap(null); });
    refreshLabels(); refreshHover();
  }, [props.poiPlaces, ready]);

  useEffect(() => {
    const map = mapRef.current;
    selectedMarker.current?.setMap(null);
    selectedMarker.current = null;
    if (!ready || !map) return;
    if (!props.selected) { popup.current?.close(); openPlaceId.current = null; return; }
    const place = props.selected.origin === 'source'
      ? props.locations.find(item => item.id === props.selected?.id) || props.selected
      : [...props.indexPlaces, ...props.poiPlaces, ...props.nearbyPlaces].find(item => item.id === props.selected?.id) || props.selected;
    let marker = [...sourceMarkers.current, ...indexMarkers.current, ...poiMarkers.current, ...nearbyMarkers.current].find(record => record.place.id === place.id)?.marker;
    if (!marker) {
      marker = createMarker(place, map);
      marker.setIcon(markerIcon(place.origin === 'source', true));
      marker.setZIndex(1000);
      selectedMarker.current = marker;
    }
    showPopup(place, marker);
  }, [props.selected, props.locations, props.indexPlaces, props.poiPlaces, props.nearbyPlaces, ready]);

  useEffect(() => {
    routeLine.current?.setMap(null);
    if (ready && props.route && mapRef.current) {
      const map = mapRef.current;
      routeLine.current = new google.maps.Polyline({ map, path: props.route.points, strokeColor: '#b92d49', strokeOpacity: 0.9, strokeWeight: 5 });
      const bounds = new google.maps.LatLngBounds();
      props.route.points.forEach(point => bounds.extend(point));
      map.fitBounds(bounds, { top: 80, bottom: 100, left: innerWidth > 760 ? 430 : 40, right: 50 });
    }
  }, [props.route, ready]);

  useEffect(() => {
    if (!ready) return;
    const dot = (color: string): google.maps.Icon => {
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="3.2" fill="${color}" stroke="#fff" stroke-width="1.4"/></svg>`;
      return { url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`, scaledSize: new google.maps.Size(14, 14), anchor: new google.maps.Point(7, 7) };
    };
    const clear = () => {
      interconnectLines.current.forEach(line => line.setMap(null));
      interconnectHandles.current.forEach(handle => handle.setMap(null));
      interconnectLines.current = [];
      interconnectHandles.current = [];
    };
    let signature = '';
    const draw = () => {
      const map = mapRef.current;
      if (!map) return;
      const centerPoint = map.getCenter();
      const center = { lat: centerPoint?.lat() ?? 0, lng: centerPoint?.lng() ?? 0 };
      const propsNow = callbacks.current;
      const graph = interconnectGraph([...propsNow.locations, ...propsNow.indexPlaces, ...propsNow.poiPlaces], center);
      const next = graph.handles.map(handle => `${handle.readout}:${handle.lat.toFixed(3)}:${handle.lng.toFixed(3)}`).join('|');
      if (next === signature) return;
      signature = next;
      clear();
      for (const edge of graph.edges) {
        interconnectLines.current.push(new google.maps.Polyline({
          map, path: edge.path, strokeColor: edge.color, strokeOpacity: 0.95, strokeWeight: 2, dashed: true,
        }));
      }
      for (const handle of graph.handles) {
        interconnectHandles.current.push(new google.maps.Marker({
          map,
          position: { lat: handle.lat, lng: handle.lng },
          title: handle.detail && handle.detail !== handle.readout ? `${handle.readout} — ${handle.detail}` : handle.readout,
          clickable: false,
          zIndex: 190,
          icon: dot(handle.color),
          label: {
            text: handle.readout,
            color: '#0f5c56',
            fontFamily: 'Manrope, sans-serif',
            fontSize: '11px',
            fontWeight: '700',
            className: 'native-place-label interconnect',
          },
        }));
      }
    };
    draw();
    const timer = window.setInterval(draw, 1000);
    const map = mapRef.current;
    const listener = map?.addListener('idle', draw);
    return () => {
      window.clearInterval(timer);
      listener?.remove();
      clear();
    };
  }, [ready]);

  return <div className="map-canvas-wrap">
    <div ref={element} className="map-canvas" aria-label="Interactive global map" />
    {props.poiPlaces.length > 0 && <div className="map-open-attribution"><a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors · ODbL</a></div>}
    {!ready && !error && <div className="map-loading"><LoaderCircle className="spin" size={26} /><span>Opening your map…</span><small>Source execution complete</small></div>}
    {error && <div className="map-error"><AlertCircle size={30} /><h2>Map unavailable</h2><p>{error}</p><button className="primary" onClick={() => { mapsPromise = null; setError(''); setReady(false); setAttempt(v => v + 1); }}><RefreshCw size={16} /> Retry map</button></div>}
  </div>;
}
