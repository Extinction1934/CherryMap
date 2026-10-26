import type { MarkerWorkerStatus } from './types';

export function markerRuntimePresentation(
  worker: MarkerWorkerStatus | null,
  connection: 'connecting' | 'connected' | 'error',
) {
  if (connection === 'error') return {
    healthy: false,
    title: 'Marker connection retrying',
    footer: 'Marker sync retrying',
    explanation: 'The current worker state cannot be confirmed while the connection is unavailable. Last valid positions remain on the map.',
  };
  if (worker && !worker.running) return {
    healthy: false,
    title: 'Python marker worker stopped',
    footer: 'Worker unavailable',
    explanation: 'Positions cannot refresh while the worker is stopped. The map retains the last valid coordinates.',
  };
  if (worker?.running && worker.status === 'error') return {
    healthy: false,
    title: 'Worker input needs attention',
    footer: 'Worker input error',
    explanation: 'The refresh task is still running, but the coordinate input was rejected. Last valid positions are retained until the input is corrected.',
  };
  if (!worker || connection !== 'connected') return {
    healthy: false,
    title: 'Connecting to Python worker',
    footer: 'Worker connecting',
    explanation: 'Waiting for a confirmed marker-worker heartbeat. No new positions are being claimed.',
  };
  return {
    healthy: true,
    title: 'Python marker worker running',
    footer: 'Worker active · 5s refresh',
    explanation: worker.coordinates_mode === 'local-updates'
      ? 'Coordinates come from the server update input. This does not verify hardware telemetry.'
      : 'The worker is active. Without new coordinate input, the source’s fixed locations remain unchanged.',
  };
}
