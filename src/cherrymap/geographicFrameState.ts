import type { MarkerFusion, MarkerSnapshot } from './types';

export function retainsGeographicFrame(snapshot: MarkerSnapshot, wantsIndex: boolean, previous: MarkerFusion | null): boolean {
  const hadGeography = previous?.markers.some(place => place.origin === 'open-index' || place.origin === 'index-cluster') || false;
  return wantsIndex && hadGeography && snapshot.index?.status !== 'ready';
}
