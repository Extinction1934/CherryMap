import type { MarkerFusion, Place } from './types';

export function fusionMarkerLayers(fusion: MarkerFusion): {
  source: Place[]; index: Place[]; provider: Place[]; poi: Place[];
} {
  const source: Place[] = [];
  const index: Place[] = [];
  const provider: Place[] = [];
  const poi: Place[] = [];
  for (const marker of fusion.markers) {
    if (marker.origin === 'source') source.push(marker);
    else if (marker.origin === 'open-index' || marker.origin === 'index-cluster') index.push(marker);
    else if (marker.origin === 'open-poi' || marker.origin === 'neural-feed') poi.push(marker);
    else if (marker.origin === 'provider' || marker.origin === 'provider-worker') provider.push(marker);
  }
  return { source, index, provider, poi };
}
