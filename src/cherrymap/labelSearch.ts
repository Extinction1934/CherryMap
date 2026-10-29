import { normalizeLongitude } from './geographicCoordinates';
import type { Place, ViewState } from './types';

export function normalizeLabel(value: string): string {
  return value.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().trim();
}

export function matchingLabels(places: Place[], query: string, center?: { lat: number; lng: number }, radius = 5000): Place[] {
  const label = normalizeLabel(query);
  if (!label) return [];
  const stop = new Set(['the', 'and', 'of', 'a', 'for', 'near', 'in', 'at', 'to', 'on']);
  const tokens = label.split(/[^0-9a-z]+/).filter(token => token.length >= 3 && !stop.has(token));
  const seen = new Set<string>();
  const inScope = (place: Place) => {
    if (!center) return true;
    const r = Math.PI / 180;
    const a = Math.sin((place.lat - center.lat) * r / 2) ** 2 + Math.cos(center.lat * r) * Math.cos(place.lat * r) * Math.sin((place.lng - center.lng) * r / 2) ** 2;
    return 6371008.8 * 2 * Math.asin(Math.min(1, Math.sqrt(a))) <= radius;
  };
  const matches = (value: string) => {
    const folded = normalizeLabel(value);
    if (!folded) return false;
    if (folded.includes(label)) return true;
    if (folded.length >= 3 && new RegExp(`(?:^|[^0-9a-z])${folded.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:$|[^0-9a-z])`).test(label)) return true;
    return tokens.some(token => new RegExp(`(?:^|[^0-9a-z])${token}(?:$|[^0-9a-z])`).test(folded));
  };
  return places.filter(place => !place.cluster && inScope(place) && [place.name, place.id, place.address || '', place.category || ''].some(matches) && !seen.has(place.id) && !!seen.add(place.id));
}

export function directSearchResult(results: Place[], query: string): Place | null {
  const label = normalizeLabel(query);
  const exact = results.filter(place => normalizeLabel(place.name) === label || normalizeLabel(place.id) === label);
  const exactSource = exact.filter(place => place.origin === 'source');
  return exactSource.length === 1 ? exactSource[0] : exact.length === 1 ? exact[0] : results.length === 1 ? results[0] : null;
}

export function labelSearchQuery(query: string, scope: 'world' | 'nearby', view: Pick<ViewState, 'lat' | 'lng'>, radius: number): string {
  const params = new URLSearchParams({ q: query.trim(), scope });
  if (scope === 'nearby') {
    const longitude = normalizeLongitude(view.lng);
    if (!Number.isFinite(view.lat) || !Number.isFinite(longitude) || Math.abs(view.lat) > 90 || !Number.isInteger(radius) || radius < 100 || radius > 20000) throw new Error('Move the map to a valid location before searching nearby.');
    params.set('lat', String(view.lat)); params.set('lng', String(longitude)); params.set('radius', String(radius));
  }
  return params.toString();
}
