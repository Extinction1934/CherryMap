import { ChevronRight, LoaderCircle, MapPin } from 'lucide-react';
import { poiCategory } from './poiPresentation';
import type { Place, POISnapshot } from './types';

export default function POILayerPanel({ snapshot, places, zoom, select }: {
  snapshot: POISnapshot | null;
  places: Place[];
  zoom: number;
  select: (place: Place) => void;
}) {
  return <section className="poi-layer-panel" aria-label="Location-specific points of interest">
    <div className="section-label"><span><span className="poi-label-dot" />POINTS OF INTEREST</span><span>{places.length.toLocaleString()}</span></div>
    <p className="poi-layer-description">All named mapped features in this area—places, natural features, transit, services, venues and more—populated by the source-extension bridge, not just cities.</p>
    {zoom < 12 || snapshot?.status === 'zoom' ? <p className="nearby-status">Zoom into a neighborhood to populate actual local POIs. Provider place tags remain clickable.</p>
      : !snapshot || snapshot.status === 'loading' ? <p className="nearby-status loading"><LoaderCircle size={12} className="spin" />The source bridge is querying local POI coverage…</p>
        : snapshot.status === 'error' ? <div className="error-box">{snapshot.message || 'The open POI query is currently unavailable.'} Provider place tags and last valid records remain usable.</div>
          : snapshot.status === 'hidden' ? <p className="nearby-status">Enable geographic data to show the open POI layer.</p>
            : <p className="nearby-status">{snapshot.loaded_count.toLocaleString()} named places from this zoom’s proximity clusters{snapshot.has_more ? ' · more pages loading' : ''}. General names only — not limited to businesses or institutions. {snapshot.stored_count.toLocaleString()} open POIs stored across queried areas.</p>}
    {places.slice(0, 8).map(place => <button key={place.id} className="place-row" onClick={() => select(place)}><span className="place-icon poi"><MapPin size={17} /></span><span className="place-copy"><strong>{place.name}</strong><small>{poiCategory(place)}</small></span><ChevronRight size={14} /></button>)}
    {snapshot?.queried_at && <p className="nearby-status">Upstream query: {new Date(snapshot.queried_at).toLocaleString()}. One-second fusion frames do not imply the underlying place changes every second.</p>}
    <p className="index-attribution"><a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors · ODbL</a>. Open POI coverage expands in queried neighborhoods; it is not a complete worldwide POI census. Provider records stay separate.</p>
  </section>;
}
