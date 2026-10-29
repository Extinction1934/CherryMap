import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  ArrowDownUp, ArrowLeft, ArrowRight, Bookmark, Check, ChevronRight,
  Code2, Compass, Crosshair, ExternalLink, Globe2, Layers, LoaderCircle,
  MapPin, Mountain, Navigation, Plus, Radio, RotateCcw, RotateCw, Route, Search, ShieldCheck,
  SlidersHorizontal, Terminal, Trash2, X, Minus, Copy, Eye, EyeOff,
} from 'lucide-react';
import MapCanvas from './MapCanvas';
import MarkerWorkerCard from './MarkerWorkerCard';
import IndexLayerPanel from './IndexLayerPanel';
import SourceExtensionOverlay from './SourceExtensionOverlay';
import POILayerPanel from './POILayerPanel';
import { fusionMarkerLayers } from './fusionMarkerLayers';
import { geographicCategory, geographicCountry } from './geographicPresentation';
import useMarkerRuntime from './useMarkerRuntime';
import { markerRuntimePresentation } from './markerRuntimePresentation';
import { directSearchResult, labelSearchQuery, matchingLabels } from './labelSearch';
import { api, coordinateText, parseCoordinates } from './api';
import type { Bootstrap, MapActions, MapStyle, Mode, Place, RouteData, ViewState } from './types';

const EMPTY_PLACES: Place[] = [];
const icons = { explore: Search, directions: Route, saved: Bookmark, source: Code2, uplinks: Radio };
const labels = { explore: 'Explore', directions: 'Directions', saved: 'Saved', source: 'Source', uplinks: 'Uplinks' };

function PlaceRow({ place, onSelect, selected }: { place: Place; onSelect: (p: Place) => void; selected?: boolean }) {
  return <button className={`place-row ${selected ? 'active' : ''}`} onClick={() => onSelect(place)}>
    <span className={`place-icon ${place.origin === 'source' ? 'source' : place.origin === 'open-index' ? 'indexed' : ''}`}><MapPin size={18} /></span>
    <span className="place-copy"><strong>{place.name}</strong><small>{place.origin === 'source' ? 'SOURCE ANNOTATION' : place.origin === 'open-index' ? `${geographicCountry(place)} · ${geographicCategory(place)}` : place.address || 'MAP SEARCH'}{place.origin === 'source' && <span className="small-dot" />}{place.origin === 'source' ? `${place.lat.toFixed(3)}, ${place.lng.toFixed(3)}` : ''}</small></span>
    <ChevronRight size={15} className="muted" />
  </button>;
}

function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty"><MapPin size={26} strokeWidth={1.5} /><strong>{title}</strong><p>{children}</p></div>;
}

function readSaved(): Place[] {
  try {
    const value = JSON.parse(localStorage.getItem('cherrymap.saved') || '[]');
    return Array.isArray(value) ? value.filter(p => typeof p.name === 'string' && typeof p.lat === 'number' && typeof p.lng === 'number').slice(0, 100) : [];
  } catch { return []; }
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [bootError, setBootError] = useState('');
  const [mode, setMode] = useState<Mode>('explore');
  const [query, setQuery] = useState('');
  const [searchScope, setSearchScope] = useState<'world' | 'nearby'>('world');
  const [searchRadius, setSearchRadius] = useState(5000);
  const searchAbort = useRef<AbortController | null>(null);
  const [providerResults, setProviderResults] = useState<Place[]>([]);
  const [searched, setSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [mapStyle, setMapStyle] = useState<MapStyle>('roadmap');
  const [sourceVisible, setSourceVisible] = useState(true);
  const [nearbyVisible, setNearbyVisible] = useState(true);
  const [indexVisible, setIndexVisible] = useState(true);
  const [selected, setSelected] = useState<Place | null>(null);
  const [view, setView] = useState<ViewState>({ lat: 20, lng: 0, zoom: 2, visibleIds: [] });
  const [mapReady, setMapReady] = useState(false);
  const [saved, setSaved] = useState<Place[]>(readSaved);
  const [origin, setOrigin] = useState('');
  const [destination, setDestination] = useState('');
  const [travelMode, setTravelMode] = useState('driving');
  const [route, setRoute] = useState<RouteData | null>(null);
  const [routing, setRouting] = useState(false);
  const [routeError, setRouteError] = useState('');
  const [toast, setToast] = useState('');
  const [dockOpen, setDockOpen] = useState(true);
  const [sourceFilter, setSourceFilter] = useState('');
  const [visibleOnly, setVisibleOnly] = useState(false);
  const mapActions = useRef<MapActions | null>(null);
  const searchVersion = useRef(0);
  const poiRequestVersion = useRef(0);
  const runtime = useMarkerRuntime(bootstrap, view, nearbyVisible, indexVisible);
  const workerDisplay = markerRuntimePresentation(runtime.worker, runtime.connection);
  const fusedLayers = useMemo(() => runtime.fusion ? fusionMarkerLayers(runtime.fusion) : null, [runtime.fusion]);
  const locations = fusedLayers?.source ?? runtime.locations ?? bootstrap?.locations ?? EMPTY_PLACES;
  const nearby = useMemo(() => ({ ...runtime.nearby, places: fusedLayers?.provider ?? runtime.nearby.places }), [runtime.nearby, fusedLayers]);
  const indexPlaces = fusedLayers?.index ?? runtime.index?.places ?? EMPTY_PLACES;
  const poiPlaces = (runtime.poi?.places && runtime.poi.places.length > 0) ? runtime.poi.places : (fusedLayers?.poi ?? EMPTY_PLACES);
  const locationsRef = useRef<Place[]>([]);
  locationsRef.current = [...locations, ...indexPlaces, ...poiPlaces, ...nearby.places];

  useEffect(() => () => { ++searchVersion.current; searchAbort.current?.abort(); }, []);

  useEffect(() => {
    api<Bootstrap>('/api/bootstrap').then(setBootstrap).catch(e => setBootError(e.message));
  }, []);
  useEffect(() => {
    try { localStorage.setItem('cherrymap.saved', JSON.stringify(saved)); }
    catch { setToast('Browser storage is unavailable. Saved places will last for this session only.'); }
  }, [saved]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 4200);
    return () => clearTimeout(timer);
  }, [toast]);

  const onMapReady = useCallback((actions: MapActions) => {
    mapActions.current = actions;
    setMapReady(true);
    setSelected(null);
    setView({ lat: 20, lng: 0, zoom: 2, visibleIds: [] });
    actions.world();
  }, []);
  const choosePlace = useCallback((place: Place) => {
    const latest = locationsRef.current.find(item => item.id === place.id) || place;
    setSelected(latest);
    if (latest.origin === 'source') setSourceVisible(true);
    if (!latest.cluster) mapActions.current?.flyTo(latest);
    setDockOpen(true);
  }, []);
  const readProviderPOI = async (id: string) => {
    const version = ++poiRequestVersion.current;
    setToast('Reading place details through the source bridge…');
    try {
      const result = await api<{ place: Place }>(`/api/place?place_id=${encodeURIComponent(id)}`);
      if (version !== poiRequestVersion.current) return;
      if (!result.place?.name || !Number.isFinite(result.place.lat) || !Number.isFinite(result.place.lng)) throw new Error('The provider returned an invalid place record.');
      setSelected(result.place); setToast('');
    } catch (error) { if (version === poiRequestVersion.current) setToast(error instanceof Error ? error.message : 'Unable to retrieve this place record.'); }
  };
  const changeMode = (next: Mode) => { setMode(next); setDockOpen(true); };
  const sourceResults = matchingLabels(locations, query, searchScope === 'nearby' ? view : undefined, searchRadius);
  const localLabelResults = matchingLabels([...indexPlaces, ...poiPlaces, ...nearby.places], query, searchScope === 'nearby' ? view : undefined, searchRadius);
  const visiblePlaces = locations.filter(place => view.visibleIds.includes(place.id));
  const filteredSource = (visibleOnly ? visiblePlaces : locations).filter(place => `${place.name} ${place.id}`.toLowerCase().includes(sourceFilter.toLowerCase()));

  useEffect(() => {
    setSelected(previous => {
      if (!previous || previous.origin === 'coordinates') return previous;
      const latest = (previous.origin === 'source' ? locations : [...indexPlaces, ...poiPlaces, ...nearby.places]).find(place => place.id === previous.id);
      if (!latest) return previous.origin === 'source' || previous.cluster ? null : previous;
      return JSON.stringify(latest) === JSON.stringify(previous) ? previous : latest;
    });
  }, [locations, indexPlaces, poiPlaces, nearby.places]);

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    searchAbort.current?.abort();
    const controller = new AbortController();
    searchAbort.current = controller;
    const version = ++searchVersion.current;
    const coordinates = parseCoordinates(query);
    setSearchError(''); setSearched(true); setProviderResults([]); setSearching(false);
    if (coordinates) {
      const place: Place = { id: `coordinates:${coordinates.lat},${coordinates.lng}`, name: `${coordinates.lat}, ${coordinates.lng}`, ...coordinates, origin: 'coordinates' };
      setIndexVisible(true); setNearbyVisible(true); setProviderResults([place]); choosePlace(place); return;
    }
    const direct = searchScope === 'world' ? directSearchResult([...sourceResults, ...localLabelResults], query) : null;
    if (direct) choosePlace(direct);
    setSearching(true);
    const timeout = setTimeout(() => controller.abort(), 18000);
    try {
      const data = await api<{ results: Place[]; warnings?: string[] }>(`/api/search?${labelSearchQuery(query, searchScope, view, searchRadius)}`, { signal: controller.signal });
      if (version !== searchVersion.current) return;
      setProviderResults(data.results);
      const warning = data.warnings?.join(' ') || '';
      setSearchError(data.results.length || localLabelResults.length || sourceResults.length ? '' : warning);
      const target = directSearchResult(data.results, query);
      if (target) choosePlace(target);
    } catch (error) {
      if (version === searchVersion.current && !localLabelResults.length && !sourceResults.length) setSearchError(controller.signal.aborted ? 'Search timed out. Please retry.' : error instanceof Error ? error.message : 'Search is unavailable.');
    } finally {
      clearTimeout(timeout);
      if (version === searchVersion.current) setSearching(false);
    }
  }
  async function getRoute(event: FormEvent) {
    event.preventDefault();
    setRouting(true); setRouteError(''); setRoute(null);
    try {
      setRoute(await api<RouteData>('/api/directions', { method: 'POST', body: JSON.stringify({ origin, destination, mode: travelMode }) }));
    } catch (e) { setRouteError((e as Error).message); }
    finally { setRouting(false); }
  }
  const savePlace = (place: Place) => {
    if (saved.some(p => p.id === place.id)) { setSaved(saved.filter(p => p.id !== place.id)); setToast('Place removed from saved.'); }
    else { setSaved([...saved, place].slice(-100)); setToast('Place saved in this browser.'); }
  };
  const locate = () => {
    if (!navigator.geolocation) { setToast('Location access is unavailable in this browser.'); return; }
    navigator.geolocation.getCurrentPosition(position => choosePlace({ id: 'my-location', name: 'Your location', lat: position.coords.latitude, lng: position.coords.longitude, origin: 'coordinates' }), () => setToast('Location access was not granted or is unavailable. Search a place instead.'), { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  };
  const expandArea = (place: Place) => { mapActions.current?.flyTo(place); setSelected(null); };
  const useDestination = (place: Place) => { setDestination(`${place.lat},${place.lng}`); changeMode('directions'); setSelected(null); };

  if (!bootstrap) return <div className="boot-screen"><img src="/favicon.svg" alt="" /><h1>CherryMap</h1>{bootError ? <><p className="error-text">{bootError}</p><p>The map cannot launch without a successful source run.</p><button className="primary" onClick={() => location.reload()}>Retry connection</button></> : <><LoaderCircle size={24} className="spin" /><p>Loading the source-ready workspace…</p></>}</div>;

  return <main className="workspace">
    <MapCanvas key="tpm119-source-bridge" bootstrap={bootstrap} locations={locations} style={mapStyle} sourceVisible={sourceVisible} nearbyVisible={nearbyVisible} nearbyPlaces={nearby.places} indexVisible={indexVisible} indexPlaces={indexPlaces} poiPlaces={poiPlaces} onProviderPlace={readProviderPOI} selected={selected} route={route} onSelect={choosePlace} onView={setView} onReady={onMapReady} onDirections={useDestination} onSave={savePlace} onExpand={expandArea} onDeselect={() => setSelected(null)} />
    <SourceExtensionOverlay worker={runtime.worker} fusion={runtime.fusion} connection={runtime.connection} error={runtime.error} index={runtime.index} poi={runtime.poi} transitioning={runtime.transitioning} openDetails={() => changeMode('source')} />
    <aside className="rail" aria-label="Main navigation">
      <button className="brand-mark" aria-label="CherryMap home" onClick={() => { changeMode('explore'); mapActions.current?.world(); }}><img src="/favicon.svg" alt="" /></button>
      <div className="rail-separator" />
      {(['explore', 'directions', 'saved', 'source', 'uplinks'] as Mode[]).map(item => {
        const Icon = icons[item];
        return <button key={item} className={`rail-button ${mode === item && dockOpen ? 'active' : ''}`} aria-label={labels[item]} title={labels[item]} onClick={() => changeMode(item)}><Icon size={21} strokeWidth={1.8} /><span>{labels[item]}</span></button>;
      })}
      <div className="rail-bottom"><button className="rail-button" aria-label="Source execution status" title="Source execution passed" onClick={() => changeMode('source')}><ShieldCheck size={21} className="green" /><span>Ready</span></button><span className="rail-version">v1.0</span></div>
    </aside>

    <section className={`dock ${dockOpen ? '' : 'collapsed'}`} aria-label={`${labels[mode]} panel`}>
      <header className="dock-header"><div><span className="wordmark">Cherry<span>Map</span></span><small>YOUR WORLD, IN CONTEXT</small></div><button className="icon-button close-dock" aria-label="Collapse panel" onClick={() => setDockOpen(false)}><X size={18} /></button></header>
      <div className="dock-content">
        {mode === 'explore' && <>
          <div className="panel-title"><span className="eyebrow">GLOBAL WORKSPACE</span><h1>Explore real places.<br />Across the world.</h1><p>Places, landmarks and points of interest through your source bridge.</p></div>
          <form className="search-form" onSubmit={search}><Search size={19} /><input aria-label="Search places, source labels, or coordinates" placeholder="Find a label, place or coordinates" maxLength={160} value={query} onChange={e => { ++searchVersion.current; searchAbort.current?.abort(); setQuery(e.target.value); setProviderResults([]); setSearched(false); setSearchError(''); setSearching(false); }} /><button aria-label="Submit search" className="search-submit" disabled={searching || !query.trim()}>{searching ? <LoaderCircle size={17} className="spin" /> : <ArrowRight size={18} />}</button></form>
          <div className="search-scope-controls"><label>Scope<select aria-label="Search scope" value={searchScope} onChange={e => { ++searchVersion.current; searchAbort.current?.abort(); setSearchScope(e.target.value as 'world' | 'nearby'); setSearched(false); setProviderResults([]); setSearchError(''); setSearching(false); }}><option value="world">Worldwide</option><option value="nearby">Near this map</option></select></label>{searchScope === 'nearby' && <label>Radius<select aria-label="Search radius" value={searchRadius} onChange={e => { ++searchVersion.current; searchAbort.current?.abort(); setSearchRadius(Number(e.target.value)); setSearched(false); setProviderResults([]); setSearching(false); }}><option value={1000}>1 km</option><option value={5000}>5 km</option><option value={20000}>20 km</option></select></label>}</div>
          <p className="search-scope-hint">{searchScope === 'nearby' ? `Within ${searchRadius / 1000} km of ${view.lat.toFixed(3)}, ${view.lng.toFixed(3)}. Pan to change the center.` : 'Search source labels, indexed places, landmarks and provider POIs. Coordinates center the map directly.'}</p>
          {!query && <div className="quick-actions"><button onClick={() => { changeMode('source'); setSourceFilter(''); }}><Code2 size={14} />Source annotations</button><button onClick={() => changeMode('directions')}><Navigation size={14} />Directions</button></div>}
          {query && <>
            <div className="section-label"><span>FROM YOUR SOURCE</span><span>{sourceResults.length}</span></div>
            {sourceResults.map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}
            {!sourceResults.length && <p className="inline-hint">No source label matches this search.</p>}
            {localLabelResults.length > 0 && <><div className="section-label"><span>AVAILABLE MAP LABELS</span><span>{localLabelResults.length}</span></div>{localLabelResults.slice(0, 12).map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}</>}
            <div className="section-label"><span>{searchScope === 'nearby' ? 'NEAR THIS MAP' : 'ALL PLACE LABELS'}</span>{searching && <LoaderCircle size={13} className="spin" />}</div>
            {!searched && <p className="inline-hint">Press Enter or the arrow to search this scope. Unique exact labels center and open directly.</p>}
            {providerResults.map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}
            {searched && !searching && !providerResults.length && !localLabelResults.length && !sourceResults.length && !searchError && <p className="inline-hint">No matches in available records. Try another name, widen the radius, or use coordinates.</p>}
            {searchError && <div className="error-box">{searchError}</div>}
          </>}
          <POILayerPanel snapshot={runtime.poi} places={poiPlaces} zoom={view.zoom} select={choosePlace} />
          <IndexLayerPanel snapshot={runtime.index} visible={indexVisible} toggle={() => setIndexVisible(!indexVisible)} select={choosePlace} transitioning={runtime.transitioning} world={() => { setIndexVisible(true); mapActions.current?.world(); }} />
          {!query && <>
            <div className="source-mini"><span className="source-mini-icon"><Layers size={21} /></span><div><strong>Source annotations</strong><small>{locations.length} code-defined locations · {runtime.worker?.coordinates_mode === 'local-updates' ? 'server coordinates' : 'source coordinates'}</small></div><button className="icon-button" aria-label="Toggle source layer" onClick={() => setSourceVisible(!sourceVisible)}>{sourceVisible ? <Eye size={17} /> : <EyeOff size={17} />}</button></div>
            <div className="section-label"><span>SOURCE ANNOTATIONS IN VIEW</span><span>{sourceVisible ? visiblePlaces.length : 'HIDDEN'}</span></div>
            {sourceVisible && visiblePlaces.slice(0, 5).map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}
            {sourceVisible && !visiblePlaces.length && <Empty title="No source locations here">Pan the map or fit the source layer to discover the coordinates in your code.</Empty>}
            {!sourceVisible && <p className="inline-hint">Source labels are hidden. Turn the layer on to see them.</p>}
            <button className="text-button view-all" onClick={() => { changeMode('source'); setVisibleOnly(false); }}>Browse source annotations <ArrowRight size={14} /></button>
          </>}
          <div className="nearby-controls"><div><strong><span className="nearby-label-dot" />Nearby places</strong><small>Python-worker refreshed · provider data</small></div><button className={`toggle ${nearbyVisible ? 'on' : ''}`} role="switch" aria-checked={nearbyVisible} aria-label="Show nearby provider places" onClick={() => setNearbyVisible(!nearbyVisible)}><span /></button></div>
          {nearby.status === 'hidden' && <p className="nearby-status">The independent GeoIndex remains active. Enable this additional layer for nearby provider places.</p>}
          {nearby.status === 'zoom' && <p className="nearby-status">Zoom to level 12 or closer to load nearby places.</p>}
          {nearby.status === 'loading' && <p className="nearby-status loading"><LoaderCircle size={12} className="spin" />Python worker is populating nearby markers…</p>}
          {nearby.status === 'error' && <div className="error-box">{nearby.message}</div>}
          {nearby.refreshedAt && <p className="nearby-status worker-nearby-status">Python worker · last refresh {new Date(nearby.refreshedAt).toLocaleTimeString()} · {nearby.refreshSeconds || 60}s provider cadence{nearby.workerRunning === false ? ' · worker unavailable' : ''}</p>}
          {nearby.coverage?.radius_meters && <p className="nearby-status">Nearby lookup radius: {(nearby.coverage.radius_meters / 1000).toFixed(1)} km · up to {nearby.coverage.result_limit} returned provider places. Not exhaustive area coverage.</p>}
          {nearby.status === 'ready' && <><div className="section-label"><span>NEARBY PROVIDER MARKERS</span><span>{nearby.places.length}</span></div>{nearby.places.map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}<p className="nearby-status">{nearby.places.length ? `${nearby.places.length} provider places shown. ` : 'No provider places returned in this area. '}{nearby.hasMore ? 'More places may be available; this is a limited provider result set.' : 'Coverage depends on the map provider.'}</p></>}
        </>}

        {mode === 'directions' && <>
          <div className="panel-title compact"><span className="eyebrow">PLAN A JOURNEY</span><h1>Take the scenic route.<br /><span className="light-heading">Or the fastest one.</span></h1></div>
          <form onSubmit={getRoute} className="directions-form"><div className="route-input"><span className="origin-dot" /><input aria-label="Starting point" placeholder="Starting point or coordinates" value={origin} onChange={e => setOrigin(e.target.value)} required maxLength={250} /></div><button type="button" className="swap-button" aria-label="Swap origin and destination" onClick={() => { setOrigin(destination); setDestination(origin); }}><ArrowDownUp size={16} /></button><div className="route-input"><MapPin size={17} className="accent" /><input aria-label="Destination" placeholder="Where are you going?" value={destination} onChange={e => setDestination(e.target.value)} required maxLength={250} /></div><div className="transport-options">{[{ id: 'driving', name: 'Drive' }, { id: 'walking', name: 'Walk' }, { id: 'bicycling', name: 'Cycle' }, { id: 'transit', name: 'Transit' }].map(item => <button type="button" key={item.id} className={travelMode === item.id ? 'selected' : ''} onClick={() => setTravelMode(item.id)}>{item.name}</button>)}</div><button className="primary" disabled={routing || !origin.trim() || !destination.trim()}>{routing ? <LoaderCircle size={16} className="spin" /> : <Route size={16} />} {routing ? 'Finding your route…' : 'Get directions'}</button></form>
          {routeError && <div className="error-box">{routeError}</div>}
          {route && <div className="route-result"><div className="route-summary"><div><strong>{route.duration}</strong><small>{route.distance} · {travelMode}</small></div><button className="icon-button" aria-label="Clear route" onClick={() => setRoute(null)}><X size={17} /></button></div><ol className="route-steps">{route.steps.map((step, index) => <li key={index}><span>{index + 1}</span><div>{step.instruction}<small>{step.distance} · {step.duration}</small></div></li>)}</ol></div>}
          {!route && !routing && <div className="note"><Compass size={18} /><p>Real routes from the map provider. Availability depends on region and travel mode.</p></div>}
        </>}

        {mode === 'saved' && <>
          <div className="panel-title compact"><span className="eyebrow">YOUR COLLECTION</span><h1>Places worth<br />coming back to.</h1><p>Saved privately in this browser.</p></div>
          <div className="section-label"><span>SAVED PLACES</span><span>{saved.length}</span></div>
          {saved.map(place => <div className="saved-row" key={place.id}><PlaceRow place={place} onSelect={choosePlace} selected={selected?.id === place.id} /><button className="icon-button" aria-label={`Remove ${place.name}`} onClick={() => setSaved(saved.filter(p => p.id !== place.id))}><Trash2 size={15} /></button></div>)}
          {!saved.length && <Empty title="Start a collection">Select a place on the map, then use Save to keep it here.</Empty>}
        </>}

        {mode === 'source' && <>
          <div className="panel-title compact"><span className="eyebrow">GROUNDED IN YOUR CODE</span><h1>Source to surface.</h1><p>Source labels. Current coordinates.</p></div>
          <div className="execution-card"><div className="execution-heading"><ShieldCheck size={20} /><strong>Source execution passed</strong><span className="pill good">EXIT 0</span></div><p>The original Python file ran successfully before this workspace launched.</p><div className="execution-stats"><div><small>PYTHON</small><strong>{bootstrap.source.python_version.split(' ')[0]}</strong></div><div><small>RUNTIME</small><strong>{(bootstrap.source.duration_ms / 1000).toFixed(2)}s</strong></div><div><small>REPORTS</small><strong>{bootstrap.source.report_count}</strong></div></div><details><summary>Execution details <ChevronRight size={13} /></summary><dl><dt>Completed (UTC)</dt><dd>{bootstrap.source.executed_at}</dd><dt>Source file</dt><dd>{bootstrap.source.filename}</dd><dt>SHA-256 fingerprint</dt><dd className="hash">{bootstrap.source.sha256}</dd><dt>Network access</dt><dd>Restricted · no live feeds</dd><dt>Disabled entry points</dt><dd>{bootstrap.source.main_guards_disabled} (preserved)</dd></dl></details></div>
          <MarkerWorkerCard worker={runtime.worker} fusion={runtime.fusion} connection={runtime.connection} error={runtime.error} transitioning={runtime.transitioning} />
          <div className="source-controls"><div><strong>Source labels</strong><small>Python-driven coordinates · no hardware attestation</small></div><button className={`toggle ${sourceVisible ? 'on' : ''}`} role="switch" aria-checked={sourceVisible} aria-label="Show source labels" onClick={() => setSourceVisible(!sourceVisible)}><span /></button></div>
          <form className="search-form small" onSubmit={e => e.preventDefault()}><Search size={17} /><input aria-label="Filter source location labels" value={sourceFilter} placeholder="Search source labels" onChange={e => setSourceFilter(e.target.value)} />{sourceFilter && <button className="icon-button" type="button" aria-label="Clear source filter" onClick={() => setSourceFilter('')}><X size={14} /></button>}</form>
          <div className="source-list-toolbar"><button className={`filter-chip ${visibleOnly ? 'active' : ''}`} onClick={() => setVisibleOnly(!visibleOnly)}><SlidersHorizontal size={12} />In map view</button><button className="text-button" disabled={!mapReady} onClick={() => mapActions.current?.fitSource()}>Fit all <Crosshair size={13} /></button></div>
          <div className="section-label"><span>{visibleOnly ? 'IN THIS MAP VIEW' : 'SOURCE LOCATION INDEX'}</span><span>{filteredSource.length}</span></div>
          {filteredSource.map(place => <PlaceRow key={place.id} place={place} onSelect={choosePlace} selected={selected?.id === place.id} />)}
          {!filteredSource.length && <Empty title="No matching source record">Try another label or show the full source index. No source label is invented for this area.</Empty>}
          <div className="tpm-state"><strong>TPM-119 sync: Unverified</strong>No authorized TPM interface or fresh signed hardware attestation has been provided. A successful source run is not TPM synchronization.</div>
          <p className="footnote">Labels become detailed at zoom 7+. Selected source labels remain visible. Source execution does not validate the code's hardware or connectivity claims.</p>
        </>}

        {mode === 'uplinks' && <>
          <div className="panel-title compact"><span className="eyebrow">READY FOR WHAT'S NEXT</span><h1>A connection<br />for another day.</h1><p>Your live-data integration workspace.</p></div>
          <div className="uplink-state"><span className="uplink-symbol"><Radio size={30} strokeWidth={1.5} /></span><span className="pill">NOT CONNECTED</span><h2>No live uplinks configured</h2><p>No hardware telemetry feed is configured. The map provider remains enabled, and the Python worker serves source records plus our independent geographic index.</p></div>
          <div className="section-label"><span>FUTURE FEED FORMAT</span><span>GEOJSON</span></div>
          <pre className="code-example">{`{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [longitude, latitude]
  },
  "properties": {
    "id": "location-id",
    "label": "Location name",
    "timestamp": "ISO-8601 UTC"
  }
}`}</pre>
          <div className="note"><ShieldCheck size={18} /><p>Connect an authorized API or WebSocket later. Keys belong on the server, never in map labels or browser code.</p></div>
        </>}
      </div>
      <footer className="dock-footer"><span><span className="status-dot" />Source ready</span><span className={`worker-network-state ${workerDisplay.healthy ? '' : 'error'}`}><Radio size={12} />{workerDisplay.footer}</span></footer>
    </section>

    {!dockOpen && <button className="reopen-dock" onClick={() => setDockOpen(true)}><Search size={18} /> Search CherryMap</button>}
    <div className="map-toolbar" aria-label="Map style"><button className={mapStyle === 'roadmap' ? 'active' : ''} onClick={() => setMapStyle('roadmap')}><Globe2 size={16} /><span>Map</span></button><button className={mapStyle === 'satellite' ? 'active' : ''} onClick={() => setMapStyle('satellite')}><Layers size={16} /><span>Satellite</span></button><button className={mapStyle === 'terrain' ? 'active' : ''} onClick={() => setMapStyle('terrain')}><Mountain size={16} /><span>Terrain</span></button></div>
    <div className="map-controls"><div className="control-group" aria-label="3D mapping controls"><button aria-label="Tilt the map up" title="Tilt up" disabled={!mapReady} onClick={() => mapActions.current?.tilt(15)}><Plus size={16} /></button><button aria-label="Tilt the map down" title="Tilt down" disabled={!mapReady} onClick={() => mapActions.current?.tilt(-15)}><Minus size={16} /></button><span className="control-divider" /><button aria-label="Rotate the map left" title="Rotate left" disabled={!mapReady} onClick={() => mapActions.current?.heading(-20)}><RotateCcw size={16} /></button><button aria-label="Rotate the map right" title="Rotate right" disabled={!mapReady} onClick={() => mapActions.current?.heading(20)}><RotateCw size={16} /></button><span className="control-divider" /><button aria-label="Level the map" title="Level view" disabled={!mapReady} onClick={() => mapActions.current?.level()}><Compass size={16} /></button></div><div className="control-group"><button aria-label="Show current location" title="My location" onClick={locate}><Navigation size={19} /></button><button aria-label="Fit all source locations" title="Fit source layer" disabled={!mapReady} onClick={() => { setSourceVisible(true); mapActions.current?.fitSource(); }}><Crosshair size={20} /></button><button aria-label="Reset world view" title="World view" disabled={!mapReady} onClick={() => mapActions.current?.world()}><Globe2 size={20} /></button></div><div className="control-group"><button aria-label="Zoom in" disabled={!mapReady} onClick={() => mapActions.current?.zoom(1)}><Plus size={20} /></button><span className="control-divider" /><button aria-label="Zoom out" disabled={!mapReady} onClick={() => mapActions.current?.zoom(-1)}><Minus size={20} /></button></div></div>
    <div className="map-legend"><span className="legend-dot" />SOURCE LOCATIONS<span className="legend-separator" />{runtime.worker?.coordinates_mode === 'local-updates' ? 'SERVER UPDATES' : 'SOURCE COORDINATES'}{indexVisible && <><span className="legend-separator" /><span className="index-label-dot" />GEONAMES INDEX</>}</div>
    <div className="coordinate-strip"><span>{coordinateText(view.lat, view.lng)}</span><span className="strip-divider" /><span>ZOOM {view.zoom}</span><button aria-label="Copy map center coordinates" onClick={() => { navigator.clipboard?.writeText(`${view.lat.toFixed(6)}, ${view.lng.toFixed(6)}`).then(() => setToast('Map center coordinates copied.')).catch(() => setToast('Clipboard access is unavailable.')); }}><Copy size={12} /></button></div>

    {selected && <section className="selection-card" aria-label="Selected location"><div className="selection-top"><span className={`pill ${selected.origin === 'source' ? 'source-pill' : ''}`}>{selected.origin === 'source' ? selected.position_origin === 'local-update' ? 'SERVER COORDINATE UPDATE' : 'STATIC SOURCE RECORD' : selected.origin === 'coordinates' ? 'COORDINATES' : selected.category === 'live-cluster' ? 'LIVE INTELLIGENCE CLUSTER' : selected.category === 'iplasma-c3-cluster' ? 'IPLASMA C3 CLUSTER' : selected.category === 'iplasma-c3' ? 'IPLASMA C3' : selected.category === 'operator-cluster' ? 'OPERATOR CLUSTER' : selected.category === 'operator' ? 'OPERATOR' : selected.category === 'portx-cluster' ? 'PORTX CLUSTER' : selected.category === 'portx' ? 'PORTX' : selected.category === 'anaconda-cluster' ? 'ANACONDA CLUSTER' : selected.category === 'anaconda' || selected.category === 'live-intelligence' ? 'ANACONDA' : selected.category === 'lattice-cluster' ? 'LATTICE PARTNER CLUSTER' : selected.category === 'restricted-lattice' ? 'LATTICE PARTNER DIRECT' : selected.cluster ? (selected.origin === 'neural-feed' ? 'NEURALLINK CLUSTER' : selected.origin === 'open-poi' ? 'BRIDGE POI CLUSTER' : 'GEOGRAPHIC INDEX CLUSTER') : selected.origin === 'open-index' ? 'OPEN GEOGRAPHIC INDEX' : selected.origin === 'open-poi' ? 'NAMED PLACE' : 'SOURCE-BRIDGED PROVIDER PLACE'}</span><button className="icon-button" aria-label="Close selected location" onClick={() => setSelected(null)}><X size={17} /></button></div><h2>{selected.name}</h2><p className="coordinate-value">{coordinateText(selected.lat, selected.lng)}</p>{selected.origin === 'source' && <p className="provenance"><Code2 size={13} />Python source · line {selected.source_line}</p>}<div className="selection-actions">{selected.cluster ? <button className="primary" onClick={() => expandArea(selected)}><Plus size={15} />Expand {selected.cluster.count.toLocaleString()} places</button> : <><button className="primary" onClick={() => useDestination(selected)}><Navigation size={15} />Directions</button><button className={`secondary ${saved.some(p => p.id === selected.id) ? 'saved' : ''}`} onClick={() => savePlace(selected)}>{saved.some(p => p.id === selected.id) ? <Check size={15} /> : <Bookmark size={15} />}{saved.some(p => p.id === selected.id) ? 'Saved' : 'Save'}</button></>}</div></section>}
    {toast && <div className="toast" role="status"><Check size={16} />{toast}</div>}
  </main>;
}
