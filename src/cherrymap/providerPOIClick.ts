export function bridgeProviderPOIClick(event: unknown, select: (id: string) => void): boolean {
  if (!event || typeof event !== 'object' || !('placeId' in event)) return false;
  const value = event as { placeId?: unknown; stop?: () => void };
  if (typeof value.placeId !== 'string' || !/^[A-Za-z0-9_-]{1,180}$/.test(value.placeId)) return false;
  value.stop?.();
  select(value.placeId);
  return true;
}
