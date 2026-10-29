import { Activity, ChevronDown, ChevronUp, Code2, Globe2, Minus, Shield } from 'lucide-react';
import { useState } from 'react';
import { readBridgeDisplay, saveBridgeDisplay } from './bridgeDisplay';
import { virtualBridgePresentation } from './virtualBridgePresentation';
import type { BridgeDisplayMode } from './bridgeDisplay';
import type { IndexSnapshot, MarkerFusion, MarkerWorkerStatus, POISnapshot } from './types';

export default function SourceExtensionOverlay({ worker, fusion, connection, index, poi, transitioning = false, error = '', openDetails }: {
  worker: MarkerWorkerStatus | null;
  fusion: MarkerFusion | null;
  connection: 'connecting' | 'connected' | 'error';
  error?: string;
  index: IndexSnapshot | null;
  poi?: POISnapshot | null;
  transitioning?: boolean;
  openDetails: () => void;
}) {
  const [mode, setMode] = useState<BridgeDisplayMode>(readBridgeDisplay);
  const bridge = virtualBridgePresentation(worker, fusion, connection, transitioning);
  const runner = worker?.runner;
  const display = (next: BridgeDisplayMode) => { setMode(next); saveBridgeDisplay(next); };
  if (!bridge.attached || !runner) return null;
  const failure = connection === 'error' ? error : '';
  const status = bridge.active ? 'connected' : connection === 'error' ? 'unconfirmed' : 'connecting';
  if (mode === 'minimized') return <button className={`source-bridge-chip ${bridge.active ? 'active' : 'attention'}`} aria-label="Restore geographic bridge" title={`${bridge.title}${failure ? ` · ${failure}` : ''} · live fusion`} onClick={() => display('compact')}><Globe2 size={15} /><span className="status-dot" /><span>Bridge · {status}</span></button>;
  const named = index?.places.filter(place => !place.cluster).length || 0;
  const clusters = index?.places.filter(place => place.cluster).length || 0;
  const bridgePois = poi?.places.filter(place => place.origin === 'open-poi').length || 0;
  const latticeFeeds = poi?.places.filter(place => place.category === 'restricted-lattice' || place.category === 'lattice-cluster').length || 0;
  const liveFeeds = poi?.places.filter(place => place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'live-cluster').length || 0;
  const operatorFeeds = poi?.places.filter(place => place.category === 'operator' || place.category === 'operator-cluster').length || 0;
  const neuralFeeds = poi?.places.filter(place => place.origin === 'neural-feed' && place.category !== 'restricted-lattice' && place.category !== 'lattice-cluster' && place.category !== 'anaconda' && place.category !== 'anaconda-cluster' && place.category !== 'live-intelligence' && place.category !== 'live-cluster' && place.category !== 'operator' && place.category !== 'operator-cluster' && place.category !== 'iplasma-c3' && place.category !== 'iplasma-c3-cluster' && place.category !== 'conscience' && place.category !== 'conscience-cluster').length || 0;
  return <aside className={`source-extension-overlay ${mode} ${bridge.active ? 'active' : 'attention'}`} aria-label="On-screen geographic data bridge" data-display-mode={mode} data-source-pid={runner.pid} data-virtual-tpm="software-only">
    <header><span className="extension-overlay-icon"><Globe2 size={16} /></span><div><strong>{mode === 'full' ? 'GeoIndex map-data bridge' : 'Source + POI bridge'}</strong><small><Activity size={10} />{status} · live fusion</small></div><button className="icon-button" aria-label="Minimize geographic bridge" title="Minimize to restore chip" onClick={() => display('minimized')}><Minus size={15} /></button><button className="icon-button" aria-label={mode === 'full' ? 'Compact geographic bridge' : 'Expand geographic bridge'} aria-expanded={mode === 'full'} onClick={() => display(mode === 'full' ? 'compact' : 'full')}>{mode === 'full' ? <ChevronUp size={15} /> : <ChevronDown size={15} />}</button></header>
    {failure && <p className="bridge-refresh-error" role="status">{failure} Retrying automatically.</p>}
    {mode === 'compact' ? <div className="bridge-compact-content"><span>{bridgePois.toLocaleString()} bridge POIs · {neuralFeeds.toLocaleString()} NeuralLink feeds · {latticeFeeds.toLocaleString()} lattice · {liveFeeds.toLocaleString()} live · {operatorFeeds.toLocaleString()} operator</span><small>{transitioning ? 'New area loading · retained frame' : bridge.syncTitle}</small>{fusion?.fused_at && <time dateTime={fusion.fused_at}>Frame {new Date(fusion.fused_at).toLocaleTimeString()}</time>}</div> : <>
      {index?.dataset && <div className="extension-geographic-total"><strong>{index.dataset.record_count.toLocaleString()}</strong><span>city/town records indexed worldwide</span><small>{transitioning ? 'Loading new area · retained geographic frame' : `${index.total_in_view.toLocaleString()} city records represented in this view`}</small><p>{named.toLocaleString()} named city markers · {clusters.toLocaleString()} coverage clusters</p></div>}
      <div id="extension-overlay-details" className="extension-overlay-details"><div className="bridge-poi-summary"><strong>{bridgePois.toLocaleString()} bridge POIs · {neuralFeeds.toLocaleString()} NeuralLink feeds · {latticeFeeds.toLocaleString()} lattice · {liveFeeds.toLocaleString()} live · {operatorFeeds.toLocaleString()} operator</strong><p>{poi?.status === 'ready' ? 'Software-bridge places, NeuralLink feeds, Restricted Lattice partner feeds, and Live Intelligence feeds in this proximity' : poi?.status === 'error' ? 'Open POI retrieval needs attention' : 'Zoom in to open this proximity'}</p>{poi?.queried_at && <small>Upstream POI retrieval: {new Date(poi.queried_at).toLocaleString()}</small>}</div><code>{runner.filename}</code><p>Original unchanged · extension in PID {runner.pid}</p>{fusion ? <><div className="extension-fusion-origins"><span>{fusion.counts.source} source annotations</span><span>{fusion.counts.provider} provider places</span></div><small>{fusion.counts.total.toLocaleString()} marker objects in {transitioning ? 'retained' : 'current'} server frame · {new Date(fusion.fused_at).toLocaleTimeString()}</small></> : <p>Waiting for this map view’s fused frame…</p>}<section className="virtual-tpm-bridge" aria-label="Virtual TPM bridge software interface"><strong><Shield size={12} />TPM-119 virtual bridge</strong><span className="virtual-mode">SOFTWARE INTERFACE ONLY</span><p>{bridge.originalChecksumVerified ? 'Original source checksum verified' : 'Source checksum awaiting verification'}</p><p className={`software-sync-result ${bridge.softwareSyncVerified ? 'verified' : 'pending'}`}>{bridge.syncTitle}</p>{bridge.syncCheckedAt && <small>Checked by source process: {new Date(bridge.syncCheckedAt).toLocaleTimeString()}</small>}<small>Continuous fusion frames from the TPM-119 software bridge. Not a hardware attestation.</small></section><div className="extension-overlay-footer"><span>Provider enabled</span><button onClick={openDetails}><Code2 size={12} />Runner details</button></div></div>
    </>}
  </aside>;
}
