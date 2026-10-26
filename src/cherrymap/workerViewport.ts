import { normalizeLongitude } from './geographicCoordinates';
import type { ViewState } from './types';

export function workerViewportKey(view: ViewState, nearbyEnabled: boolean, indexEnabled: boolean): string {
  const query = new URLSearchParams();
  const bounds = view.bounds;
  // 0.002° buckets. Finer rounding changed the key on tile-load jitter and
  // the client aborted every neighborhood response before pins could draw.
  const quantum = 500;
  const quantize = (value: number, limit: number, toward: 'down' | 'up') => {
    const stepped = toward === 'down' ? Math.floor(value * quantum) / quantum : Math.ceil(value * quantum) / quantum;
    return String(Math.max(-limit, Math.min(limit, stepped)));
  };
  if (indexEnabled && bounds && [bounds.south, bounds.north, bounds.west, bounds.east].every(Number.isFinite)) {
    query.set('index', 'true');
    query.set('south', quantize(bounds.south, 90, 'down'));
    query.set('north', quantize(bounds.north, 90, 'up'));
    query.set('west', quantize(bounds.west, 180, 'down'));
    query.set('east', quantize(bounds.east, 180, 'up'));
  }
  const longitude = normalizeLongitude(view.lng);
  if (nearbyEnabled && view.zoom >= 12 && Number.isFinite(view.lat) && Math.abs(view.lat) <= 90 && Number.isFinite(longitude)) {
    query.set('nearby', 'true');
    const step = (value: number) => (Math.round(value * quantum) / quantum).toFixed(3);
    query.set('lat', step(view.lat));
    query.set('lng', step(longitude));
    const radius = Math.min(5000, Math.max(500, Math.round((view.radius || 1500) / 500) * 500));
    query.set('radius', String(radius));
  }
  if (!query.size) return '';
  query.set('zoom', String(Math.max(2, Math.min(20, Math.round(view.zoom)))));
  return `?${query}`;
}
