import type { Place } from './types';

export interface MarkerRecord {
  marker: google.maps.Marker;
  place: Place;
}

export function reconcileSourceMarkers(
  records: MarkerRecord[],
  places: Place[],
  create: (place: Place) => google.maps.Marker,
  remove: (record: MarkerRecord) => void,
): MarkerRecord[] {
  const remaining = new Map(records.map(record => [record.place.id, record]));
  const next = places.map(place => {
    const record = remaining.get(place.id);
    if (!record) return { place, marker: create(place) };
    remaining.delete(place.id);
    if (record.place.lat !== place.lat || record.place.lng !== place.lng) {
      record.marker.setPosition({ lat: place.lat, lng: place.lng });
    }
    const title = place.marker?.title || place.name;
    if (record.marker.getTitle() !== title) record.marker.setTitle(title);
    record.place = place;
    return record;
  });
  remaining.forEach(remove);
  return next;
}
