/** Keep an internal repeated-world coordinate at its equivalent geographic longitude. */
export function normalizeLongitude(longitude: number): number {
  if (!Number.isFinite(longitude)) return NaN;
  if (longitude >= -180 && longitude <= 180) return longitude;
  return ((longitude + 180) % 360 + 360) % 360 - 180;
}

/** JavaScript remainder is negative for negative operands; wrap any number of worlds. */
export function wrapWorldOffset(offset: number, worldWidth: number): number {
  if (!Number.isFinite(offset) || !Number.isFinite(worldWidth) || worldWidth <= 0) return NaN;
  return ((offset + worldWidth / 2) % worldWidth + worldWidth) % worldWidth - worldWidth / 2;
}
