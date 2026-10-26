import type { Place } from './types';

export interface InterconnectEdge {
  path: { lat: number; lng: number }[];
  color: string;
}

export interface InterconnectHandle {
  lat: number;
  lng: number;
  readout: string;
  detail: string;
  color: string;
}

export interface InterconnectGraph {
  edges: InterconnectEdge[];
  handles: InterconnectHandle[];
}

const COLORS = ['#b92d49', '#396b94', '#1e6b48', '#5c3d8f', '#1a4fbf', '#e36a12', '#0f766e'];

function ring(handles: InterconnectHandle[]): InterconnectEdge[] {
  if (handles.length < 2) return [];
  const edges: InterconnectEdge[] = [];
  const last = handles.length >= 3 ? handles.length : handles.length - 1;
  for (let index = 0; index < last; index++) {
    const a = handles[index];
    const b = handles[(index + 1) % handles.length];
    edges.push({ path: [{ lat: a.lat, lng: a.lng }, { lat: b.lat, lng: b.lng }], color: COLORS[index % COLORS.length] });
  }
  if (handles.length >= 6) {
    for (const [from, to, color] of [[0, 2, COLORS[2]], [2, 4, COLORS[4]], [4, 0, COLORS[0]]] as const) {
      edges.push({ path: [{ lat: handles[from].lat, lng: handles[from].lng }, { lat: handles[to].lat, lng: handles[to].lng }], color });
    }
  }
  return edges;
}

export function interconnectGraph(places: Place[], center: { lat: number; lng: number }): InterconnectGraph {
  const handles = places
    .filter(place => place.category === 'interconnect-fusion' && Number.isFinite(place.lat) && Number.isFinite(place.lng))
    .map(place => ({
      lat: place.lat,
      lng: place.lng,
      readout: (place.name || '').trim(),
      detail: (place.address || place.marker?.popup.provenance || '').trim(),
      color: '#0f766e',
    }))
    .filter(handle => handle.readout.length > 0)
    .sort((a, b) => Math.atan2(a.lng - center.lng, a.lat - center.lat) - Math.atan2(b.lng - center.lng, b.lat - center.lat));
  return { edges: ring(handles), handles };
}
