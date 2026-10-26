import type { Place } from './types';

const countries = new Intl.DisplayNames(['en'], { type: 'region' });
export function geographicCountry(place: Place): string {
  const code = place.country_code || place.address;
  if (!code) return '';
  if (/^[A-Z]{2}$/.test(code)) {
    try { return countries.of(code) || code; } catch { return code; }
  }
  return code;
}

export function geographicCategory(place: Place): string {
  if (place.category === 'PPLC') return 'Capital city';
  if (place.category?.startsWith('PPLA')) return 'Administrative seat';
  if (place.category?.startsWith('PPL')) return 'Populated place';
  return 'Indexed geographic place';
}

export function geographicPopulation(place: Place): string | null {
  return typeof place.population === 'number' && Number.isSafeInteger(place.population) && place.population >= 0
    ? place.population.toLocaleString('en-US') : null;
}

export function compactCoverage(count: number): string {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(count);
}
