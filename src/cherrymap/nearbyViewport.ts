import { normalizeLongitude } from './geographicCoordinates';
import type { NearbySnapshot, NearbyState, ViewState } from './types';

export function nearbyViewportKey(view: ViewState, enabled: boolean): string {
  if (!enabled) return 'hidden';
  if (view.zoom < 12) return 'zoom';
  const longitude = normalizeLongitude(view.lng);
  if (!Number.isFinite(view.lat) || Math.abs(view.lat) > 90 || !Number.isFinite(longitude)) return 'zoom';
  const radius = Math.min(5000, Math.max(100, Math.round((view.radius || 1500) / 50) * 50));
  return `?${new URLSearchParams({ nearby: 'true', lat: view.lat.toFixed(4), lng: longitude.toFixed(4), radius: String(radius) })}`;
}

export function nearbyLayerState(snapshot: NearbySnapshot): NearbyState {
  return {
    status: snapshot.status, places: snapshot.places, hasMore: snapshot.has_more,
    message: snapshot.message, refreshedAt: snapshot.last_refresh_at,
    refreshSeconds: snapshot.refresh_seconds, workerRunning: snapshot.worker_running,
    coverage: snapshot.coverage,
  };
}
