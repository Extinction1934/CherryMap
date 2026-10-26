import { Activity, AlertCircle, RefreshCw } from 'lucide-react';
import { markerRuntimePresentation } from './markerRuntimePresentation';
import type { MarkerFusion, MarkerWorkerStatus } from './types';

export default function MarkerWorkerCard({ worker, fusion, connection, error, transitioning = false }: {
  worker: MarkerWorkerStatus | null;
  fusion?: MarkerFusion | null;
  connection: 'connecting' | 'connected' | 'error';
  error: string;
  transitioning?: boolean;
}) {
  const display = markerRuntimePresentation(worker, connection);
  const attention = connection === 'error' || worker?.status === 'error' || (worker !== null && !worker.running);
  const runner = worker?.runner;
  return <section className={`worker-card ${display.healthy ? 'healthy' : 'attention'}`} aria-label="Python marker runtime" data-cycle-count={worker?.cycle_count} data-runner-kind={runner?.kind} data-runner-pid={runner?.pid}>
    <header>{display.healthy ? <Activity size={17} /> : attention ? <AlertCircle size={17} /> : <RefreshCw size={17} className="spin" />}<strong>{display.title}</strong></header>
    <div className="worker-summary"><span>{worker?.interval_seconds ?? 5}s refresh</span><span>{worker?.cycle_count ?? 0} cycles</span><span>Revision {worker?.revision ?? '—'}</span></div>
    {runner?.kind === 'cherrypro-source-extension' && <div className="source-runner-identity"><strong>Unchanged source + in-process extension</strong><code>{runner.filename}</code><small>Attached extension: {runner.extension_filename}</small><p>PID {runner.pid} · started {new Date(runner.started_at).toLocaleTimeString()}</p><small title={runner.sha256}>Original source SHA-256: {runner.sha256.slice(0, 16)}…</small></div>}
    {worker?.last_refresh_at && <p className="worker-time">Last check: {new Date(worker.last_refresh_at).toLocaleTimeString()} · {worker.changed_count} source marker updates this cycle</p>}
    {runner?.kind === 'cherrypro-source-extension' && <div className="source-fusion-status"><strong>Server-side marker fusion</strong>{fusion ? <><p>{fusion.counts.source} source · {fusion.counts.index} index · {fusion.counts.provider} provider · {fusion.counts.total} fused markers</p><small>Fusion frame: {new Date(fusion.fused_at).toLocaleTimeString()}</small>{worker?.sync && <p>Software pipeline sync: {transitioning ? 'pending new area' : connection === 'connected' && worker.running ? worker.sync.status : 'unconfirmed'} · not hardware attestation</p>}</> : <p>Waiting for the attached extension’s fused frame for this view.</p>}<p className="fusion-boundary">Indexed records and provider observations are not a verified sensor feed. TPM-119 remains Unverified.</p></div>}
    <p>{display.explanation}</p>
    {(error || worker?.error) && <p className="error-text">{error || worker?.error} Last valid positions are retained.</p>}
  </section>;
}
