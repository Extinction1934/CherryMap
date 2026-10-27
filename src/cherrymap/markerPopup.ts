import { coordinateText } from './api';
import { compactCoverage, geographicCategory, geographicCountry, geographicPopulation } from './geographicPresentation';
import { poiCategory, safePOIWebsite } from './poiPresentation';
import type { Place } from './types';

export function markerPopup(
  place: Place,
  actions?: { directions: (place: Place) => void; save: (place: Place) => void; expand?: (place: Place) => void },
): HTMLElement {
  const root = document.createElement('article');
  root.className = 'marker-popup';
  root.dataset.placeId = place.id;
  root.setAttribute('aria-label', `${place.name} location details`);

  const badge = document.createElement('span');
  badge.className = `marker-popup-badge ${place.origin === 'source' ? 'source' : 'provider'}`;
  badge.textContent = place.origin === 'source' ? place.position_origin === 'local-update' ? 'SERVER COORDINATE UPDATE' : 'STATIC SOURCE LOCATION' : place.origin === 'coordinates' ? 'COORDINATES' : place.category === 'operator' || place.category === 'operator-cluster' ? 'OPERATOR' : place.category === 'portx' || place.category === 'portx-cluster' ? 'PORTX' : place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence' ? 'ANACONDA' : place.category === 'live-cluster' ? 'LIVE INTELLIGENCE CLUSTER' : place.category === 'lattice-cluster' ? 'LATTICE PARTNER CLUSTER' : place.category === 'restricted-lattice' ? 'LATTICE PARTNER DIRECT' : place.cluster ? (place.origin === 'neural-feed' ? 'NEURALLINK CLUSTER' : place.origin === 'open-poi' ? 'BRIDGE POI CLUSTER' : 'GEOGRAPHIC INDEX CLUSTER') : place.origin === 'open-index' ? 'OPEN GEOGRAPHIC INDEX' : place.origin === 'neural-feed' ? 'NEURALLINK INTERNAL FEED' : place.origin === 'open-poi' ? 'NAMED PLACE' : 'SOURCE-BRIDGED PROVIDER PLACE';
  if (place.category === 'portx' || place.category === 'portx-cluster') badge.className = 'marker-popup-badge portx';
  if (place.category === 'operator' || place.category === 'operator-cluster') badge.className = 'marker-popup-badge operator';
  if (place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence') badge.className = 'marker-popup-badge live';
  const heading = document.createElement('h2');
  heading.textContent = place.marker?.popup.heading || place.name;
  const coordinates = document.createElement('p');
  coordinates.className = 'marker-popup-coordinates';
  coordinates.textContent = coordinateText(place.lat, place.lng);
  root.append(badge, heading, coordinates);

  if (place.address) {
    const address = document.createElement('p');
    address.className = 'marker-popup-address';
    address.textContent = place.origin === 'open-index' ? geographicCountry(place) : place.address;
    root.append(address);
  }
  if (place.origin === 'open-index') {
    const details = document.createElement('dl');
    details.className = 'marker-popup-geography';
    for (const [key, value] of [
      ['Place type', geographicCategory(place)],
      ['Population (dataset)', geographicPopulation(place)],
      ['GeoIndex ID', place.id.replace('geonames:', '')],
    ]) {
      if (value === null) continue;
      const term = document.createElement('dt'); term.textContent = key;
      const description = document.createElement('dd'); description.textContent = value;
      details.append(term, description);
    }
    root.append(details);
  }
  if (place.origin === 'open-poi' || place.origin === 'provider-worker') {
    const details = document.createElement('dl');
    details.className = 'marker-popup-geography';
    for (const [key, value] of [['Type', poiCategory(place)], ['Brand', place.brand], ['Opening hours (record)', place.opening_hours]]) {
      if (!value) continue;
      const term = document.createElement('dt'); term.textContent = key || '';
      const description = document.createElement('dd'); description.textContent = value;
      details.append(term, description);
    }
    root.append(details);
    const website = safePOIWebsite(place.website);
    if (website) {
      const link = document.createElement('a');
      link.href = website; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = 'Place website'; root.append(link);
    }
  }
  const provenance = document.createElement('p');
  provenance.className = 'marker-popup-provenance';
  provenance.textContent = place.origin === 'source'
    ? `${place.marker?.popup.provenance || 'Static Python source location — not live telemetry'} · line ${place.marker?.popup.source_line || place.source_line}`
    : place.origin === 'provider' || place.origin === 'provider-worker'
      ? place.marker?.popup.provenance || 'Nearby place data supplied by the map provider, not the Python source.'
      : place.origin === 'open-index' || place.cluster
        ? place.marker?.popup.provenance || 'GeoNames · CC BY 4.0 · application-owned geographic index'
      : place.origin === 'neural-feed' ? place.marker?.popup.provenance || 'CherryPro NeuralLink internal feed'
      : place.origin === 'open-poi' ? place.marker?.popup.provenance || 'OpenStreetMap contributors · ODbL · source-bridge POI index'
      : 'Selected geographic coordinates.';
  root.append(provenance);
  if (place.observed_at) {
    const observed = document.createElement('p');
    observed.className = 'marker-popup-provenance';
    observed.textContent = `Input observation: ${place.observed_at}`;
    root.append(observed);
  }
  if (place.refreshed_at) {
    const refreshed = document.createElement('p');
    refreshed.className = 'marker-popup-provenance';
    refreshed.textContent = `Worker refreshed: ${new Date(place.refreshed_at).toLocaleTimeString()}`;
    root.append(refreshed);
  }
  if (place.queried_at) {
    const queried = document.createElement('p');
    queried.className = 'marker-popup-provenance';
    queried.textContent = `Upstream retrieved: ${new Date(place.queried_at).toLocaleString()}`;
    root.append(queried);
  }
  if (place.origin === 'open-poi') {
    const attribution = document.createElement('a');
    attribution.href = 'https://www.openstreetmap.org/copyright';
    attribution.target = '_blank'; attribution.rel = 'noreferrer';
    attribution.textContent = '© OpenStreetMap contributors · ODbL'; root.append(attribution);
  }
  if (place.origin === 'open-index' || place.cluster) {
    const attribution = document.createElement('a');
    attribution.href = 'https://www.geonames.org/';
    attribution.target = '_blank'; attribution.rel = 'noreferrer';
    attribution.textContent = 'GeoNames · CC BY 4.0';
    root.append(attribution);
  }
  if (place.data_modified_at) {
    const modified = document.createElement('p');
    modified.className = 'marker-popup-provenance';
    modified.textContent = `Dataset record modified: ${place.data_modified_at}`;
    root.append(modified);
  }
  if (place.category === 'anaconda' || place.category === 'anaconda-cluster' || place.category === 'live-intelligence') {
    const details = document.createElement('dl');
    details.className = 'marker-popup-geography';
    for (const [key, value] of [
      ['Plug-in', 'Anaconda'],
      ['Lattice', 'NeuralLink'],
      ['Fusion', place.observed_at || 'in cycle'],
    ]) {
      const term = document.createElement('dt'); term.textContent = key;
      const description = document.createElement('dd'); description.textContent = value;
      details.append(term, description);
    }
    root.append(details);
  }
  if (place.cluster) {
    const count = document.createElement('p');
    count.className = 'marker-popup-provenance';
    count.textContent = place.category === 'operator-cluster'
      ? `${place.cluster.count.toLocaleString()} operator feeds in this cell. Expand to open them.`
      : place.category === 'portx-cluster'
      ? `${place.cluster.count.toLocaleString()} PortX signals in this cell. Expand to open them.`
      : place.category === 'anaconda-cluster'
      ? `${place.cluster.count.toLocaleString()} feeds in this fusion cell. Expand to open the cell.`
      : place.category === 'live-cluster'
      ? `${place.cluster.count.toLocaleString()} Restricted Lattice live intelligence feeds in this fusion cell. Expand to open the cell.`
      : place.category === 'lattice-cluster'
      ? `${place.cluster.count.toLocaleString()} Restricted Lattice partner feeds in this fusion cell. Expand to open the cell.`
      : place.origin === 'neural-feed'
      ? `${place.cluster.count.toLocaleString()} NeuralLink feeds in this fusion cell. Expand to open the cell.`
      : place.origin === 'open-poi'
        ? `${place.cluster.count.toLocaleString()} software-bridge places in this fusion cell. Expand to open the cell.`
        : `${place.cluster.count.toLocaleString()} indexed records in this geographic cell. Expand to reveal individual places.`;
    root.append(count);
    if (actions?.expand) {
      const expand = document.createElement('button');
      expand.className = 'primary'; expand.type = 'button';
      expand.textContent = 'Expand this area'; expand.onclick = () => actions.expand?.(place);
      root.append(expand);
    }
    return root;
  }

  if (actions) {
    const row = document.createElement('div');
    row.className = 'marker-popup-actions';
    const directions = document.createElement('button');
    directions.type = 'button';
    directions.textContent = 'Directions';
    directions.className = 'primary';
    directions.onclick = () => actions.directions(place);
    const save = document.createElement('button');
    save.type = 'button';
    save.textContent = 'Save place';
    save.className = 'secondary';
    save.onclick = () => actions.save(place);
    row.append(directions, save);
    root.append(row);
  }
  return root;
}

export function markerIcon(source: boolean, selected = false, sourceColor?: string): google.maps.Icon {
  const color = sourceColor && /^#[0-9a-f]{6}$/i.test(sourceColor) ? sourceColor : source ? '#b92d49' : '#458579';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="36" height="44" viewBox="0 0 36 44"><path d="M18 42S3 26 3 18a15 15 0 1 1 30 0c0 8-15 24-15 24Z" fill="${color}" stroke="#fff" stroke-width="2.5"/><circle cx="18" cy="18" r="5" fill="#fff"/></svg>`;
  const width = selected ? 34 : source ? 29 : 24;
  const height = width * 44 / 36;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new google.maps.Size(width, height),
    anchor: new google.maps.Point(width / 2, height),
    labelOrigin: new google.maps.Point(width / 2, height + 11),
  };
}

export function clusterMarkerIcon(count: number, selected = false, color?: string): google.maps.Icon {
  const stroke = color && /^#[0-9a-f]{6}$/i.test(color) ? color : '#467da6';
  const size = selected ? 46 : count >= 1000 ? 40 : 34;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44" viewBox="0 0 44 44"><circle cx="22" cy="22" r="19" fill="#ffffff" fill-opacity=".92" stroke="${stroke}" stroke-width="3"/></svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new google.maps.Size(size, size),
    anchor: new google.maps.Point(size / 2, size / 2),
    labelOrigin: new google.maps.Point(size / 2, size / 2),
  };
}

export const clusterMarkerLabel = compactCoverage;

export function geographicOverviewIcon(selected = false): google.maps.Icon {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><circle cx="12" cy="12" r="${selected ? 7 : 4}" fill="#467da6" stroke="#fff" stroke-width="1.5"/></svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new google.maps.Size(24, 24),
    anchor: new google.maps.Point(12, 12),
    labelOrigin: new google.maps.Point(12, 26),
  };
}
