export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const validation = Array.isArray(data.detail) ? data.detail.slice(0, 3).flatMap((item: { msg?: unknown; loc?: unknown }) => {
      if (typeof item?.msg !== 'string') return [];
      const location = Array.isArray(item.loc) ? item.loc.at(-1) : null;
      const field = typeof location === 'string' && /^[a-z_]{1,32}$/.test(location) ? `${location}: ` : '';
      return [`${field}${item.msg.slice(0, 180)}`];
    }).join('; ') : '';
    const message = typeof data.detail === 'string'
      ? data.detail
      : validation || 'The request could not be completed. Please try again.';
    throw new Error(`Request failed (HTTP ${response.status}): ${message}`);
  }
  return data as T;
}

export function parseCoordinates(value: string): { lat: number; lng: number } | null {
  const match = value.trim().match(/^([+-]?\d+(?:\.\d+)?)\s*[,;]\s*([+-]?\d+(?:\.\d+)?)$/);
  if (!match) return null;
  const lat = Number(match[1]);
  const lng = Number(match[2]);
  return Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? { lat, lng } : null;
}

export function coordinateText(lat: number, lng: number): string {
  return `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}  ·  ${Math.abs(lng).toFixed(4)}° ${lng >= 0 ? 'E' : 'W'}`;
}
