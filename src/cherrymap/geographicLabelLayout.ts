import type { Place } from './types';

type ScreenPoint = { x: number; y: number };

export function geographicLabelIds(
  places: Place[],
  project: (place: Place) => ScreenPoint | null,
  selectedId: string | null,
): Set<string> {
  const labels = new Set<string>();
  const occupied = new Map<string, ScreenPoint[]>();
  const ranked = places.filter(place => !place.cluster && (place.origin === 'open-index' || place.origin === 'open-poi')).slice().sort((a, b) =>
    Number(b.id === selectedId) - Number(a.id === selectedId)
    || Number(b.category === 'PPLC') - Number(a.category === 'PPLC')
    || (b.population || 0) - (a.population || 0)
    || a.id.localeCompare(b.id));
  for (const place of ranked) {
    const point = project(place);
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
    const cellX = Math.floor(point.x / 90);
    const cellY = Math.floor(point.y / 28);
    let overlaps = false;
    for (let x = cellX - 1; x <= cellX + 1; x++) {
      for (let y = cellY - 1; y <= cellY + 1; y++) {
        if (occupied.get(`${x}:${y}`)?.some(previous => Math.abs(previous.x - point.x) < 90 && Math.abs(previous.y - point.y) < 28)) overlaps = true;
      }
    }
    if (overlaps && place.id !== selectedId) continue;
    labels.add(place.id);
    const key = `${cellX}:${cellY}`;
    occupied.set(key, [...(occupied.get(key) || []), point]);
  }
  return labels;
}
