export type BridgeDisplayMode = 'full' | 'compact' | 'minimized';
export const BRIDGE_DISPLAY_KEY = 'cherrymap.bridgeDisplay';

export function readBridgeDisplay(storage?: Pick<Storage, 'getItem'>): BridgeDisplayMode {
  try {
    const value = (storage || localStorage).getItem(BRIDGE_DISPLAY_KEY);
    return value === 'full' || value === 'compact' || value === 'minimized' ? value : 'compact';
  } catch { return 'compact'; }
}

export function saveBridgeDisplay(value: BridgeDisplayMode, storage?: Pick<Storage, 'setItem'>): void {
  try { (storage || localStorage).setItem(BRIDGE_DISPLAY_KEY, value); } catch { /* Session-only fallback. */ }
}

export function nextMarkerPollDelay(elapsedMs: number, failed = false): number {
  // One fresh frame about every second. Polling the full street feed several
  // times a second was enough to freeze the preview before pins could stay up.
  return failed ? 900 : Math.max(650, 1100 - elapsedMs);
}

const CAMERA_KEY = 'cherrymap.camera';

export function readSavedCamera(): { lat: number; lng: number; zoom: number } | null {
  try {
    const raw = JSON.parse(sessionStorage.getItem(CAMERA_KEY) || '');
    if (!raw || !Number.isFinite(raw.lat) || !Number.isFinite(raw.lng) || !Number.isFinite(raw.zoom)) return null;
    if (raw.zoom < 2 || raw.zoom > 20 || Math.abs(raw.lat) > 90) return null;
    return { lat: raw.lat, lng: raw.lng, zoom: raw.zoom };
  } catch {
    return null;
  }
}

export function saveCamera(view: { lat: number; lng: number; zoom: number }) {
  try {
    if (!Number.isFinite(view.lat) || !Number.isFinite(view.lng) || !Number.isFinite(view.zoom)) return;
    if (view.zoom < 2 || view.zoom > 20 || Math.abs(view.lat) > 90) return;
    const previous = readSavedCamera();
    if (previous && previous.zoom > 4 && view.zoom <= 2) return;
    sessionStorage.setItem(CAMERA_KEY, JSON.stringify({ lat: view.lat, lng: view.lng, zoom: view.zoom }));
  } catch { /* The map still keeps its current camera in memory. */ }
}
