import { ChevronRight, Globe2, LoaderCircle, MapPin } from 'lucide-react';
import { geographicCountry } from './geographicPresentation';
import type { IndexSnapshot, Place } from './types';

export default function IndexLayerPanel({ snapshot, visible, toggle, select, transitioning = false, world }: {
  snapshot: IndexSnapshot | null;
  visible: boolean;
  toggle: () => void;
  select: (place: Place) => void;
  transitioning?: boolean;
  world?: () => void;
}) {
  const named = snapshot?.places.filter(place => !place.cluster) || [];
  const clusters = snapshot?.places.filter(place => place.cluster) || [];
  const examples = [...named].sort((a, b) => Number(b.category === 'PPLC') - Number(a.category === 'PPLC') || (b.population || 0) - (a.population || 0));
  return <section className="geographic-index-panel" aria-label="Actual GeoIndex map data">
    <div className="nearby-controls"><div><strong><span className="index-label-dot" />Geographic map data</strong><small>Populated by the source-extension worker</small></div><button className={`toggle ${visible ? 'on' : ''}`} role="switch" aria-checked={visible} aria-label="Show own geographic index" onClick={toggle}><span /></button></div>
    {snapshot?.dataset && <div className="global-index-total"><strong>{snapshot.dataset.record_count.toLocaleString()}</strong><span>indexed places worldwide</span><small>{snapshot.dataset.name} · actual database records</small></div>}
    {world && <button className="text-button index-world-button" onClick={world}><Globe2 size={13} />Explore worldwide map data</button>}
    {!visible ? <p className="nearby-status">Geographic markers are hidden. The provider remains enabled.</p>
      : !snapshot || snapshot.status === 'hidden' || (snapshot.status === 'loading' && !snapshot.places.length)
        ? <p className="nearby-status loading"><LoaderCircle size={12} className="spin" />The source worker is populating geographic places…</p>
        : <>
          {transitioning && <p className="index-transition loading"><LoaderCircle size={12} className="spin" />Loading the new area · previous geographic frame stays visible</p>}
          {snapshot.status === 'error' && <p className="error-box">{snapshot.message || 'The geographic index is temporarily unavailable.'} Last valid geographic data stays visible.</p>}
          <div className="index-coverage"><strong>{snapshot.total_in_view.toLocaleString()}</strong><span>{transitioning ? 'records represented in the retained frame' : 'indexed records represented in this map view'}</span></div>
          <p className="nearby-status">{snapshot.mode === 'overview'
            ? `${named.length.toLocaleString()} actual named places · ${clusters.length.toLocaleString()} coverage clusters. Zoom or expand a cluster for local place detail.`
            : snapshot.mode === 'clusters' ? `${clusters.length.toLocaleString()} interactive coverage clusters. Expand an area to reveal named place markers.`
            : `${snapshot.loaded_count.toLocaleString()} of ${snapshot.total_in_view.toLocaleString()} individual place markers loaded.`}</p>
          {snapshot.mode === 'overview' && snapshot.coverage && <p className="index-overview-rule">Named overview: capitals and populated places {snapshot.coverage.min_population ? `with population ≥ ${snapshot.coverage.min_population.toLocaleString()}` : 'in this view'}. Coverage clusters represent the complete index, including smaller places. Named points are already counted within cluster coverage.</p>}
          {snapshot.has_more && <p className="nearby-status loading"><LoaderCircle size={12} className="spin" />Loading further matching named places—no provider page cap.</p>}
          {examples.slice(0, 6).map(place => <button key={place.id} className="place-row" onClick={() => select(place)}><span className="place-icon indexed"><MapPin size={17} /></span><span className="place-copy"><strong>{place.name}</strong><small>{geographicCountry(place)}{place.category === 'PPLC' ? ' · CAPITAL' : ''}</small></span><ChevronRight size={14} /></button>)}
          {!examples.length && clusters.slice(0, 3).map(place => <button key={place.id} className="place-row" onClick={() => select(place)}><span className="place-icon indexed"><MapPin size={17} /></span><span className="place-copy"><strong>{place.name}</strong><small>Coverage cluster · expand area</small></span><ChevronRight size={14} /></button>)}
          {snapshot.refreshed_at && <p className="nearby-status">Geographic refresh: {new Date(snapshot.refreshed_at).toLocaleTimeString()}{!snapshot.worker_running ? ' · worker unavailable' : ''}</p>}
        </>}
    <p className="index-attribution"><a href="https://www.geonames.org/" target="_blank" rel="noreferrer">GeoNames</a> · CC BY 4.0 · cities, towns and administrative seats. Not every business/building or live sensor telemetry. Provider basemap and search stay enabled.</p>
  </section>;
}
