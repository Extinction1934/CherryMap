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
