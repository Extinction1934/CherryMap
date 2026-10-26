import type { MarkerFusion, MarkerWorkerStatus } from './types';

export function virtualBridgePresentation(
  worker: MarkerWorkerStatus | null,
  fusion: MarkerFusion | null,
  connection: 'connecting' | 'connected' | 'error',
  transitioning = false,
) {
  const runner = worker?.runner;
  const attached = runner?.kind === 'cherrypro-source-extension';
  const active = Boolean(attached && connection === 'connected' && worker?.running && worker.status === 'running');
  const title = connection === 'error' ? 'Software session unconfirmed'
    : !attached ? 'Awaiting source extension'
    : !worker?.running ? 'Software session stopped'
    : worker.status === 'error' ? 'Software input needs attention'
    : active ? 'Software session attached' : 'Connecting';
  const originalChecksumVerified = Boolean(attached && /^[0-9a-f]{64}$/.test(runner.sha256) && runner.sha256 === runner.original_sha256);
  const softwareSyncVerified = Boolean(active && fusion && !transitioning && originalChecksumVerified
    && worker?.sync?.scope === 'software-marker-pipeline' && worker.sync.verified
    && worker.sync.hardware_verified === false && worker.sync.sensor_verified === false);
  return {
    mode: 'virtual-software-interface' as const,
    hardwareVerified: false as const,
    sensorVerified: false as const,
    attached,
    active,
    title,
    originalChecksumVerified,
    softwareSyncVerified,
    syncTitle: !active ? 'Software sync unconfirmed'
      : transitioning ? 'Loading geographic view · sync pending'
      : softwareSyncVerified ? 'Software sync verified'
      : worker?.sync?.status === 'degraded' ? 'Software sync needs attention'
      : 'Waiting for complete software sync',
    syncCheckedAt: active && fusion && !transitioning ? worker?.sync?.checked_at : undefined,
    failedChecks: active ? worker?.sync?.failed_checks ?? [] : [],
    fusionCount: fusion?.counts.total ?? null,
  };
}
