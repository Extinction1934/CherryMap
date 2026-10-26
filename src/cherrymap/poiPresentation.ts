import type { Place } from './types';

export function poiCategory(place: Place): string {
  const raw = place.category || 'point of interest';
  return raw.replace(/[_:]/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

export function safePOIWebsite(value?: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return (url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
