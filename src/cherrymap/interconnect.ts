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

const COLORS = ['#b92d49', '#396b94', '#1e6b48', '#5c3d8f', '#1a4fbf', '#e36a12', '#0f766e', '#ca8a04'];

const ANCHORS: { color: string; test: (place: Place) => boolean }[] = [
  { color: '#5c3d8f', test: place => place.category === 'neural-feed' || place.category === 'neural-cluster' },
  { color: '#1e6b48', test: place => place.category === 'restricted-lattice' || place.category === 'lattice-cluster' },
  { color: '#1a4fbf', test: place => place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' || place.category === 'live-cluster' },
  { color: '#e36a12', test: place => place.category === 'portx' || place.category === 'portx-cluster' },
  { color: '#be185d', test: place => place.category === 'operator' || place.category === 'operator-cluster' },
  { color: '#d946ef', test: place => place.category === 'iplasma-c3' || place.category === 'iplasma-c3-cluster' },
  { color: '#0369a1', test: place => place.category === 'aicp' || place.category === 'aicp-cluster' },
  { color: '#0f766e', test: place => place.category === 'interconnect-fusion' },
  { color: '#24312c', test: place => place.origin === 'open-poi' },
  { color: '#396b94', test: place => place.origin === 'open-index' || place.origin === 'index-cluster' },
  { color: '#b92d49', test: place => place.origin === 'source' },
];

function nearest(origin: { lat: number; lng: number }, anchors: { lat: number; lng: number }[]) {
  let best = anchors[0];
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const anchor of anchors) {
    const distance = (anchor.lat - origin.lat) ** 2 + (anchor.lng - origin.lng) ** 2;
    if (distance < bestDistance) {
      best = anchor;
      bestDistance = distance;
    }
  }
  return best;
}

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

export function interconnectGraph(places: Place[], _center: { lat: number; lng: number }): InterconnectGraph {
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
    .sort((a, b) => a.lat - b.lat || a.lng - b.lng);
  const conscience = places
    .filter(place => (place.category === 'conscience' || place.category === 'conscience-cluster' || place.category === 'aicp' || place.category === 'aicp-cluster') && Number.isFinite(place.lat) && Number.isFinite(place.lng))
    .map(place => ({ lat: place.lat, lng: place.lng, conscience: place.category === 'conscience' || place.category === 'conscience-cluster' }))
    .sort((a, b) => a.lat - b.lat || a.lng - b.lng);
  const edges = ring(handles);
  const conscienceHandles = conscience.map(pin => ({ lat: pin.lat, lng: pin.lng, readout: '', detail: '', color: pin.conscience ? '#ca8a04' : '#0369a1' }));
  edges.push(...ring(conscienceHandles));
  const families = ANCHORS.map(anchor => ({
    color: anchor.color,
    points: places.filter(place => anchor.test(place) && Number.isFinite(place.lat) && Number.isFinite(place.lng)).map(place => ({ lat: place.lat, lng: place.lng })),
  })).filter(family => family.points.length > 0);
  for (const pin of conscience) {
    if (!pin.conscience) continue;
    for (const family of families) {
      const anchor = nearest(pin, family.points);
      if (Math.abs(anchor.lat - pin.lat) < 1e-6 && Math.abs(anchor.lng - pin.lng) < 1e-6) continue;
      edges.push({ path: [{ lat: pin.lat, lng: pin.lng }, { lat: anchor.lat, lng: anchor.lng }], color: family.color });
    }
  }
  return { edges, handles };
}
