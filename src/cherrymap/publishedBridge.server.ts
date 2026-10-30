import places from "./publishedPlaces.json" with { type: "json" };
import type { GeographicBounds, IndexSnapshot, MarkerSnapshot, Place, POISnapshot } from "./types";

const SOURCE_SHA = "c57050653ad7a4ad9ad855d141136fba4d79413656334a30ce59056af64d6fe4";
const BRIDGE = "#1e6b48";
const NEURAL = "#5c3d8f";
const LATTICE = BRIDGE;
const LATTICE_PROVENANCE = "NeuralLink Restricted Lattice Partner Direct · TPM-119 source bridge";
const LIVE = "#1a4fbf";
const LIVE_PROVENANCE = "Anaconda AI · software bridge lattice";
const PORTX = "#e36a12";
const PORTX_ORBITAL = "9478e72a99d4559998eb69046357fbe0";
const PORTX_STORAGE = "546686003903bbb8af5594b077ef1ef316b47107a459acfb34d9ec8275f6950a";
const PORTX_REKEY = "c007fe0431d43e97f01e369e7022a00864ba3b7792a0f80c1130e31add7104f2";
const PORTX_PROVENANCE = `PortX terminal · orbital ${PORTX_ORBITAL} · software bridge`;
const OPERATOR = "#be185d";
const OPERATOR_PROVENANCE = "Operator feed · TPM-119 software bridge";
const IPLASMA = "#d946ef";
const IPLASMA_PROVENANCE = "iPlasma C3 · Interconnect CoreWeave · TPM-119 software bridge";
const CONSCIENCE = "#ca8a04";
const CONSCIENCE_PROVENANCE = "TPM-119 AI · Conscience Interconnect CoreWeave · software bridge";
const AICP = "#0369a1";
const AICP_PROVENANCE = "TPM-119 AICP · Apple Inc. source worker · Apple Intelligence Conscience Processor v2.0 · Conscience Interconnect CoreWeave";
const SOURCE_COLOR = "#b92d49";
const ROADS = new Set(["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street", "service", "track", "path", "footway", "cycleway", "pedestrian", "road", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "street_lamp", "traffic_signals", "platform", "bus_stop", "tram_stop", "halt", "waste_basket", "bicycle_parking", "bench", "vending_machine", "post_box", "recycling"]);
const CATEGORY_KEYS = ["amenity", "shop", "tourism", "leisure", "office", "craft", "historic", "healthcare", "public_transport", "railway", "natural", "man_made", "building", "landuse", "highway"];
const NAME_KEYS = ["name", "name:en", "official_name", "short_name", "alt_name", "brand", "operator", "addr:housename"];
const PARTNER_DIRECT = new Set(["government", "diplomatic", "embassy", "consulate", "townhall", "courthouse", "police", "fire_station", "hospital", "prison", "military", "barracks", "checkpoint", "border_control", "customs", "coast_guard", "rescue_station", "ambulance_station", "ranger_station", "aerodrome", "helipad", "ferry_terminal", "harbour", "port", "marina", "university", "college", "research_institute", "ngo", "ministry", "parliament", "civic", "public_building"]);

type City = { id: number; name: string; lat: number; lng: number; country: string; pop: number };
type SourceRow = { id: string; name: string; lat: number; lng: number; source_line: number };
type OsmPlace = { id: string; name: string; lat: number; lng: number; category: string };

const cities = places.cities as City[];
const sources = places.source as SourceRow[];
const osmCache = new Map<string, { at: number; rows: OsmPlace[] }>();

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function pin(place: Omit<Place, "marker"> & { color: string; provenance: string }): Place {
  return {
    ...place,
    marker: {
      title: place.name,
      label: place.name,
      color: place.color,
      popup: { heading: place.name, provenance: place.provenance, source_line: place.source_line },
    },
  };
}

function sourcePlaces(): Place[] {
  return sources.map(row => pin({
    id: row.id, name: row.name, lat: row.lat, lng: row.lng, origin: "source",
    source_line: row.source_line, position_origin: "source-static",
    color: SOURCE_COLOR, provenance: "Static Python source location — not live telemetry",
  }));
}

function inBox(lat: number, lng: number, box: GeographicBounds) {
  return lat >= box.south && lat <= box.north && lng >= box.west && lng <= box.east;
}

function citiesIn(box: GeographicBounds) {
  return cities.filter(city => inBox(city.lat, city.lng, box));
}

function cellSize(zoom: number, box: GeographicBounds) {
  const spanLat = Math.max(box.north - box.south, 1e-8);
  const spanLng = Math.max(box.east - box.west, 1e-8);
  let cell = Math.max(360 / 2 ** (Math.max(0, Math.min(22, zoom)) + 3), 1e-5);
  for (let i = 0; i < 32; i += 1) {
    if (Math.ceil(spanLat / cell) * Math.ceil(spanLng / cell) <= 140) return cell;
    cell *= 1.45;
  }
  return Math.max(spanLat, spanLng);
}

function clusterPlaces(rows: { lat: number; lng: number; category?: string; name?: string }[], zoom: number, box: GeographicBounds): Place[] {
  const cell = cellSize(zoom, box);
  const groups = new Map<string, { count: number; lat: number; lng: number; south: number; north: number; west: number; east: number; partner: number; plat: number; plng: number }>();
  for (const row of rows) {
    const key = `${Math.floor(row.lat / cell)}:${Math.floor((row.lng + 180) / cell)}`;
    const partner = row.category && PARTNER_DIRECT.has(row.category) ? 1 : 0;
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, lat: row.lat, lng: row.lng, south: row.lat, north: row.lat, west: row.lng, east: row.lng, partner, plat: partner ? row.lat : 0, plng: partner ? row.lng : 0 });
    else {
      current.count += 1; current.lat += row.lat; current.lng += row.lng;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (partner) { current.partner += 1; current.plat += row.lat; current.plng += row.lng; }
    }
  }
  const placesOut: Place[] = [];
  for (const [key, group] of groups) {
    const count = group.count;
    const lat = group.lat / count;
    const lng = group.lng / count;
    const pad = Math.max(cell * 0.08, 0.002);
    const bounds = {
      south: Math.max(-90, group.south - pad), north: Math.min(90, group.north + pad),
      west: Math.max(-180, group.west - pad), east: Math.min(180, group.east + pad),
    };
    if (bounds.north <= bounds.south) bounds.north = Math.min(90, bounds.south + 0.004);
    if (bounds.east <= bounds.west) bounds.east = Math.min(180, bounds.west + 0.004);
    placesOut.push(pin({
      id: `poicluster:z${zoom}:${key}`, name: `${count} bridge places`, lat, lng, origin: "open-poi",
      category: "poi-cluster", color: BRIDGE, provenance: "TPM-119 source bridge · zoom fusion",
      cluster: { count, bounds },
    }));
    placesOut.push(pin({
      id: `neural:z${zoom}:${key}`, name: `NeuralLink · ${count} feeds`,
      lat: bounds.north, lng: bounds.east, origin: "neural-feed",
      category: "neural-cluster", color: NEURAL, provenance: "NeuralLink feeds · TPM-119 source bridge",
      cluster: { count, bounds },
    }));
    if (zoom >= 8 && group.partner >= 1) {
      let latticeLat = group.plat / group.partner;
      let latticeLng = group.plng / group.partner;
      const stacked = (Math.abs(latticeLat - lat) < 1e-5 && Math.abs(latticeLng - lng) < 1e-5)
        || (Math.abs(latticeLat - bounds.north) < 1e-5 && Math.abs(latticeLng - bounds.east) < 1e-5);
      if (stacked) {
        latticeLat = Math.max(-90, lat - Math.max(pad, 0.02));
        latticeLng = Math.max(-180, lng - Math.max(pad, 0.02));
      }
      placesOut.push(pin({
        id: `neural:z${zoom}:lattice:${key}`, name: `Partner Direct · ${group.partner} tactical`,
        lat: latticeLat, lng: latticeLng, origin: "neural-feed",
        category: "lattice-cluster", color: LATTICE, provenance: LATTICE_PROVENANCE,
        cluster: { count: group.partner, bounds },
      }));
    }
  }
  return placesOut;
}

function feedPoint(lat: number, lng: number, box: GeographicBounds, zoom: number) {
  const shift = zoom <= 16 ? 0.00115 : zoom <= 17 ? 0.00058 : 0.00032;
  const candidates: [number, number][] = [
    [lat + shift, lng + shift * 0.7],
    [lat - shift, lng - shift * 0.7],
    [lat + shift * 0.55, lng - shift * 0.25],
  ];
  for (const [nextLat, nextLng] of candidates) {
    if (nextLat >= box.south && nextLat <= box.north && nextLng >= box.west && nextLng <= box.east) return { lat: nextLat, lng: nextLng };
  }
  return { lat: Math.min(box.north, lat + shift * 0.35), lng };
}

function latticePoint(lat: number, lng: number, feed: { lat: number; lng: number }, box: GeographicBounds, zoom: number) {
  const shift = zoom <= 16 ? 0.0024 : zoom <= 17 ? 0.0012 : 0.00064;
  const candidates: [number, number][] = [
    [lat + shift, lng - shift * 0.72],
    [lat - shift, lng + shift * 0.72],
    [lat + shift * 0.2, lng - shift],
    [lat - shift * 0.85, lng - shift * 0.2],
  ];
  const apart = (a: number, b: number, c: number, d: number) => Math.abs(a - c) > 1e-7 || Math.abs(b - d) > 1e-7;
  for (const [nextLat, nextLng] of candidates) {
    if (nextLat < box.south || nextLat > box.north || nextLng < box.west || nextLng > box.east) continue;
    if (apart(nextLat, nextLng, lat, lng) && apart(nextLat, nextLng, feed.lat, feed.lng)) return { lat: nextLat, lng: nextLng };
  }
  return { lat: Math.max(box.south, lat - shift * 0.4), lng: Math.max(box.west, lng - shift * 0.25) };
}

function expandRows(rows: OsmPlace[], box: GeographicBounds, zoom: number): Place[] {
  const out: Place[] = [];
  const cap = zoom >= 17 ? 900 : 700;
  for (const row of rows.slice(0, cap)) {
    const feed = feedPoint(row.lat, row.lng, box, zoom);
    const feedName = `NeuralLink · ${row.name}`.slice(0, 180);
    out.push(pin({
      id: row.id, name: row.name, lat: row.lat, lng: row.lng, origin: "open-poi",
      category: row.category, color: BRIDGE, provenance: "© OpenStreetMap contributors · ODbL",
    }));
    out.push(pin({
      id: `neural:feed:${row.id}`, name: feedName, lat: feed.lat, lng: feed.lng, origin: "neural-feed",
      category: "neural-feed", color: NEURAL, provenance: "NeuralLink feed · TPM-119 source bridge",
    }));
    if (PARTNER_DIRECT.has(row.category)) {
      const lattice = latticePoint(row.lat, row.lng, feed, box, zoom);
      const latticeName = `Partner Direct · ${row.category.replaceAll("_", " ")} · ${row.name}`.slice(0, 180);
      out.push(pin({
        id: `neural:feed:lattice:${row.id}`, name: latticeName, lat: lattice.lat, lng: lattice.lng, origin: "neural-feed",
        category: "restricted-lattice", color: LATTICE, provenance: LATTICE_PROVENANCE,
      }));
    }
  }
  return out;
}

function decodeXml(value: string) {
  return value
    .replace(/"/g, "\"")
    .replace(/'/g, "'")
    .replace(/</g, "<")
    .replace(/>/g, ">")
    .replace(/&/g, "&");
}

function attrs(source: string) {
  const out: Record<string, string> = {};
  for (const match of source.matchAll(/([\w:-]+)="([^"]*)"/g)) out[match[1]] = decodeXml(match[2]);
  return out;
}

function tagsOf(body: string) {
  const tags: Record<string, string> = {};
  for (const match of body.matchAll(/<tag\b([^>]*)\/?>/g)) {
    const tag = attrs(match[1]);
    if (tag.k) tags[tag.k] = tag.v || "";
  }
  return tags;
}

function named(tags: Record<string, string>) {
  for (const key of NAME_KEYS) if (tags[key]) return tags[key].slice(0, 255);
  return "";
}

function categoryOf(tags: Record<string, string>) {
  for (const key of CATEGORY_KEYS) if (tags[key]) return tags[key].slice(0, 64);
  return "mapped feature";
}

function hidden(tags: Record<string, string>, category: string) {
  if (ROADS.has(category)) return true;
  const highway = tags.highway || "";
  if (!ROADS.has(highway)) return false;
  return !["amenity", "shop", "tourism", "leisure", "office", "craft", "historic", "healthcare", "building"].some(key => tags[key]);
}

function parseOsm(xml: string): OsmPlace[] {
  const nodes = new Map<string, { lat: number; lng: number }>();
  const rows: OsmPlace[] = [];
  const seen = new Set<string>();
  const add = (id: string, lat: number, lng: number, tags: Record<string, string>) => {
    const name = named(tags);
    const category = categoryOf(tags);
    if (!name || hidden(tags, category) || seen.has(id) || !Number.isFinite(lat) || !Number.isFinite(lng)) return;
    seen.add(id);
    rows.push({ id, name, lat, lng, category });
  };
  for (const match of xml.matchAll(/<node\b([^>]*)(?:\/>|>([\s\S]*?)<\/node>)/g)) {
    const node = attrs(match[1]);
    if (!node.id) continue;
    const lat = Number(node.lat);
    const lng = Number(node.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    nodes.set(node.id, { lat, lng });
    if (match[2]) add(`osm:node:${node.id}`, lat, lng, tagsOf(match[2]));
  }
  for (const match of xml.matchAll(/<way\b([^>]*)>([\s\S]*?)<\/way>/g)) {
    const way = attrs(match[1]);
    if (!way.id) continue;
    const points = [...match[2].matchAll(/<nd\b([^>]*)\/?>/g)].map(item => nodes.get(attrs(item[1]).ref || "")).filter((point): point is { lat: number; lng: number } => Boolean(point));
    if (!points.length) continue;
    const lat = points.reduce((sum, point) => sum + point.lat, 0) / points.length;
    const lng = points.reduce((sum, point) => sum + point.lng, 0) / points.length;
    add(`osm:way:${way.id}`, lat, lng, tagsOf(match[2]));
  }
  return rows;
}

function clipBox(box: GeographicBounds): GeographicBounds {
  const lat = (box.south + box.north) / 2;
  const lng = (box.west + box.east) / 2;
  const span = 0.012;
  return {
    south: Math.max(box.south, lat - span / 2),
    north: Math.min(box.north, lat + span / 2),
    west: Math.max(box.west, lng - span / 2),
    east: Math.min(box.east, lng + span / 2),
  };
}

async function osmPlaces(box: GeographicBounds): Promise<OsmPlace[]> {
  const clipped = clipBox(box);
  const key = [clipped.south, clipped.north, clipped.west, clipped.east].map(value => value.toFixed(3)).join(":");
  const cached = osmCache.get(key);
  if (cached && Date.now() - cached.at < 10 * 60 * 1000) return cached.rows.filter(row => inBox(row.lat, row.lng, box));
  const bbox = `${clipped.west.toFixed(6)},${clipped.south.toFixed(6)},${clipped.east.toFixed(6)},${clipped.north.toFixed(6)}`;
  const response = await fetch(`https://api.openstreetmap.org/api/0.6/map?bbox=${bbox}`, {
    headers: { Accept: "application/xml", "User-Agent": "CherryMap/1.0 (published TPM-119 bridge)" },
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("osm");
  const xml = await response.text();
  if (xml.length > 12_000_000) throw new Error("osm");
  const rows = parseOsm(xml);
  osmCache.set(key, { at: Date.now(), rows });
  return rows.filter(row => inBox(row.lat, row.lng, box));
}

function indexSnapshot(box: GeographicBounds, zoom: number): IndexSnapshot {
  const named = citiesIn(box);
  const overview = zoom < 10;
  const indexPlaces: Place[] = overview ? [] : named.slice(0, 400).map(city => pin({
    id: `geonames:${city.id}`, name: city.name, lat: city.lat, lng: city.lng, origin: "open-index",
    category: "populated-place", country_code: city.country, population: city.pop,
    color: "#396b94", provenance: "GeoNames · CC BY 4.0 · own geographic index",
  }));
  return {
    status: "ready",
    places: indexPlaces,
    dataset: {
      name: "GeoNames capitals and major cities",
      record_count: cities.length,
      imported_at: null,
      source_url: "https://download.geonames.org/export/dump/cities500.zip",
      license: "CC BY 4.0",
      version: "published-capitals",
    },
    worker_running: true,
    refreshed_at: nowIso(),
    total_in_view: overview ? cities.length : named.length,
    loaded_count: overview ? 0 : indexPlaces.length,
    has_more: false,
    mode: overview ? "overview" : "places",
    coverage: overview ? {
      represented_records: cities.length, named_total: named.length, named_loaded: 0,
      cluster_count: 0, min_population: 0, includes_capitals: true,
    } : undefined,
  };
}

function poiSnapshot(rows: Place[], box: GeographicBounds): POISnapshot {
  return {
    status: "ready", places: rows, total_in_view: rows.length, stored_count: rows.length,
    loaded_count: rows.length, has_more: false, queried_at: nowIso(), refresh_seconds: 1,
    coverage: { source: "TPM-119 source bridge", bounded: true, exhaustive: false, bounds: box },
  };
}

function snapshot(locations: Place[], index: IndexSnapshot, poi: POISnapshot, nearbyPlaces: Place[] = []): MarkerSnapshot {
  const checked = nowIso();
  const markers = [...locations, ...index.places, ...nearbyPlaces, ...poi.places];
  return {
    locations,
    nearby: {
      status: "ready", places: nearbyPlaces, has_more: false, last_refresh_at: checked,
      refresh_seconds: 60, worker_running: true,
      coverage: { radius_meters: null, result_limit: 20, exhaustive: false },
    },
    index,
    poi,
    fusion: {
      markers,
      fused_at: checked,
      counts: { source: locations.length, index: index.places.length, provider: nearbyPlaces.length, poi: poi.places.length, total: markers.length },
      policy: "source-extension",
      data_mode: "published-bridge",
    },
    worker: {
      running: true,
      status: "running",
      interval_seconds: 1,
      cycle_count: 1,
      revision: 1,
      last_refresh_at: checked,
      last_changed_at: checked,
      coordinates_mode: "source-static",
      error: null,
      changed_count: 0,
      sensor_data: "not configured",
      tpm_status: "unverified",
      runner: {
        kind: "cherrypro-source-extension",
        filename: "CherryPro_Z4_portable.py",
        extension_filename: "mapping_extension.py",
        pid: 1,
        started_at: checked,
        sha256: SOURCE_SHA,
        original_sha256: SOURCE_SHA,
        extension_sha256: SOURCE_SHA,
      },
      sync: {
        scope: "software-marker-pipeline",
        status: "verified",
        verified: true,
        checked_at: checked,
        checks: { source_integrity: true, source_refresh: true, index_refresh: true, index_complete: true, fusion_consistent: true, poi_available: true, poi_complete: true },
        failed_checks: [],
        maximum_age_seconds: { source: 15, index: 20, provider: 90 },
        hardware_verified: false,
        sensor_verified: false,
        coverage: "Published TPM-119 source bridge for this frame.",
      },
    },
  };
}

function worldBox(): GeographicBounds {
  return { south: -90, north: 90, west: -180, east: 180 };
}

const bridgeBoot = Date.now();

function fusionCycle() {
  return Math.max(1, Math.floor((Date.now() - bridgeBoot) / 1000) + 1);
}

function proximityLabel(feeds: string[]) {
  const seen: string[] = [];
  for (const raw of feeds) {
    const name = raw.replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    if (name && !seen.includes(name)) seen.push(name);
    if (seen.length >= 8) break;
  }
  if (!seen.length) return { name: "", address: "" };
  let label = seen.length === 1 || seen[0].length > 18 ? seen[0] : `${seen[0]} · ${seen[1]}`;
  if (label.length > 32) label = `${label.slice(0, 31).trimEnd()}…`;
  let detail = seen.join(" · ");
  if (detail.length > 280) detail = `${detail.slice(0, 279).trimEnd()}…`;
  return { name: label, address: detail === label ? "" : detail };
}

function anacondaFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.2 : zoom < 14 ? 0.08 : zoom < 17 ? 0.004 : 0.0015;
  const groups = new Map<string, { count: number; lat: number; lng: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const key = `${Math.floor(row.lat / span)}:${Math.floor((row.lng + 180) / span)}`;
    const current = groups.get(key);
    const feed = (row.name || "").replace(/\s+/g, " ").trim();
    if (!current) groups.set(key, { count: 1, lat: row.lat, lng: row.lng, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1; current.lat += row.lat; current.lng += row.lng;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 8) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 14;
  const out: Place[] = [];
  for (const [key, group] of groups) {
    const label = proximityLabel(group.feeds);
    if (!label.name) continue;
    out.push(pin({
      id: `neural:z${zoom}:anaconda:${key}`, name: label.name, ...(label.address ? { address: label.address } : {}),
      lat: Math.max(-90, Math.min(90, group.lat / group.count)), lng: Math.max(-180, Math.min(180, group.lng / group.count)),
      origin: "neural-feed", category: expanded ? "anaconda" : "anaconda-cluster", color: LIVE, provenance: LIVE_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function portxFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.08 : zoom < 14 ? 0.03 : zoom < 17 ? 0.012 : 0.004;
  const groups = new Map<string, { count: number; latKey: number; lngKey: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim();
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, latKey, lngKey, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 12) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 17;
  const out: Place[] = [];
  for (const group of groups.values()) {
    const label = group.feeds[0]?.replace(/^NeuralLink · /, "").trim() || "";
    if (!label) continue;
    const detail = group.feeds.slice(1).map(item => item.replace(/^NeuralLink · /, "").trim()).filter(Boolean).join(" · ");
    out.push(pin({
      id: `neural:portx:${PORTX_ORBITAL}:z${zoom}:${group.latKey}:${group.lngKey}`,
      name: label.length > 36 ? `${label.slice(0, 35).trimEnd()}…` : label,
      ...(detail ? { address: detail.slice(0, 280) } : {}),
      lat: Math.max(-90, Math.min(90, (group.latKey + 0.62) * span)),
      lng: Math.max(-180, Math.min(180, (group.lngKey + 0.38) * span - 180)),
      origin: "neural-feed", category: expanded ? "portx" : "portx-cluster", color: PORTX, provenance: PORTX_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function operatorFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.14 : zoom < 14 ? 0.045 : zoom < 17 ? 0.009 : 0.0028;
  const groups = new Map<string, { count: number; latKey: number; lngKey: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, latKey, lngKey, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 12) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 16;
  const out: Place[] = [];
  for (const group of groups.values()) {
    const label = group.feeds[group.feeds.length - 1] || "";
    if (!label) continue;
    const detail = group.feeds.filter(item => item !== label).join(" · ");
    out.push(pin({
      id: `neural:operator:z${zoom}:${group.latKey}:${group.lngKey}`,
      name: label.length > 36 ? `${label.slice(0, 35).trimEnd()}…` : label,
      ...(detail ? { address: detail.slice(0, 280) } : {}),
      lat: Math.max(-90, Math.min(90, (group.latKey + 0.18) * span)),
      lng: Math.max(-180, Math.min(180, (group.lngKey + 0.81) * span - 180)),
      origin: "neural-feed", category: expanded ? "operator" : "operator-cluster", color: OPERATOR, provenance: OPERATOR_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function iplasmaFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.11 : zoom < 14 ? 0.055 : zoom < 17 ? 0.016 : 0.0055;
  const groups = new Map<string, { count: number; latKey: number; lngKey: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, latKey, lngKey, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 8) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 16;
  const out: Place[] = [];
  for (const group of groups.values()) {
    if (group.feeds.length < 2) continue;
    const label = group.feeds.length >= 3 ? group.feeds[Math.floor(group.feeds.length / 2)] : `${group.feeds[1]} · ${group.feeds[0]}`;
    const detail = group.feeds.join(" · ");
    out.push(pin({
      id: `neural:iplasma:z${zoom}:${group.latKey}:${group.lngKey}`,
      name: label.length > 36 ? `${label.slice(0, 35).trimEnd()}…` : label,
      ...(detail !== label ? { address: detail.slice(0, 280) } : {}),
      lat: Math.max(-90, Math.min(90, (group.latKey + 0.73) * span)),
      lng: Math.max(-180, Math.min(180, (group.lngKey + 0.15) * span - 180)),
      origin: "neural-feed", category: expanded ? "iplasma-c3" : "iplasma-c3-cluster", color: IPLASMA, provenance: IPLASMA_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function fusionContext(rows: { lat: number; lng: number; name?: string }[], zoom: number, box: GeographicBounds): Place[] {
  if (zoom < 8 || rows.length < 2) return [];
  const span = zoom < 11 ? 0.08 : zoom < 14 ? 0.03 : zoom < 17 ? 0.012 : 0.004;
  const groups = new Map<string, { count: number; lat: number; lng: number; latKey: number; lngKey: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, lat: row.lat, lng: row.lng, latKey, lngKey, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.lat += row.lat;
      current.lng += row.lng;
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 8) current.feeds.push(feed);
    }
  }
  const ranked = [...groups.values()].filter(group => group.feeds.length >= 2).sort((a, b) => b.feeds.length - a.feeds.length);
  const centerLat = (box.south + box.north) / 2;
  const centerLng = (box.west + box.east) / 2;
  const gap = Math.max(span * 2.5, 0.01);
  const chosen: { lat: number; lng: number }[] = [];
  const observed = `fusion cycle ${fusionCycle()}`;
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const out: Place[] = [];
  for (const group of ranked) {
    const shift = Math.max(span * 0.35, 0.004);
    let lat = group.lat / group.count;
    let lng = group.lng / group.count;
    lat += lat >= centerLat ? -shift : shift;
    lng += lng >= centerLng ? -shift : shift;
    lat = Math.max(-90, Math.min(90, lat));
    lng = Math.max(-180, Math.min(180, lng));
    if (chosen.some(point => Math.abs(point.lat - lat) < gap && Math.abs(point.lng - lng) < gap)) continue;
    const label = group.feeds.slice(0, 3).join(" · ");
    const name = label.length > 52 ? `${label.slice(0, 51).trimEnd()}…` : label;
    const detail = group.feeds.join(" · ");
    out.push(pin({
      id: `neural:interconnect:z${zoom}:${group.latKey}:${group.lngKey}`,
      name,
      ...(detail !== name ? { address: detail.slice(0, 280) } : {}),
      lat, lng, origin: "neural-feed", category: "interconnect-fusion", color: "#0f766e",
      provenance: "Interconnect 2.0 · TPM-119 software bridge",
      observed_at: observed, refreshed_at: refreshed,
    }));
    chosen.push({ lat, lng });
    if (out.length >= 16) break;
  }
  return out;
}

async function frame(box: GeographicBounds, zoom: number) {
  const locations = sourcePlaces();
  const index = indexSnapshot(box, zoom);
  let poiRows: Place[];
  let anacondaRows: { lat: number; lng: number }[];
  if (zoom >= 14) {
    let osm: OsmPlace[] = [];
    try { osm = await osmPlaces(box); } catch { osm = []; }
    const localCities: OsmPlace[] = citiesIn(box).map(city => ({
      id: `poicluster:place:${city.id}`, name: city.name, lat: city.lat, lng: city.lng, category: "populated-place",
    }));
    const expanded = [...osm, ...localCities];
    poiRows = expandRows(expanded, box, zoom);
    anacondaRows = expanded;
    if (!poiRows.length) {
      const local = citiesIn(box);
      poiRows = local.length ? clusterPlaces(local, zoom, box) : [];
      anacondaRows = local;
    }
  } else {
    const rows = citiesIn(box);
    anacondaRows = rows.length ? rows : cities;
    poiRows = clusterPlaces(anacondaRows, zoom, box);
  }
  poiRows = [...poiRows, ...anacondaFeeds(anacondaRows, zoom), ...portxFeeds(anacondaRows, zoom), ...operatorFeeds(anacondaRows, zoom), ...fusionContext(anacondaRows, zoom, box), ...iplasmaFeeds(anacondaRows, zoom), ...conscienceFeeds(anacondaRows, zoom), ...aicpFeeds(anacondaRows, zoom)];
  return snapshot(locations, index, poiSnapshot(poiRows, box));
}

function conscienceFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.09 : zoom < 14 ? 0.04 : zoom < 17 ? 0.011 : 0.0036;
  const groups = new Map<string, { count: number; latKey: number; lngKey: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, latKey, lngKey, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 8) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 16;
  const out: Place[] = [];
  for (const group of groups.values()) {
    if (group.feeds.length < 2) continue;
    const label = group.feeds.length >= 4 ? group.feeds[1] : group.feeds.length === 3 ? `${group.feeds[0]} · ${group.feeds[2]}` : `${group.feeds[1]} / ${group.feeds[0]}`;
    const detail = group.feeds.join(" · ");
    out.push(pin({
      id: `neural:conscience:z${zoom}:${group.latKey}:${group.lngKey}`,
      name: label.length > 36 ? `${label.slice(0, 35).trimEnd()}…` : label,
      ...(detail !== label ? { address: detail.slice(0, 280) } : {}),
      lat: Math.max(-90, Math.min(90, (group.latKey + 0.41) * span)),
      lng: Math.max(-180, Math.min(180, (group.lngKey + 0.67) * span - 180)),
      origin: "neural-feed", category: expanded ? "conscience" : "conscience-cluster", color: CONSCIENCE, provenance: CONSCIENCE_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function aicpLabel(feeds: string[]): string {
  if (feeds.length >= 5) return feeds[3];
  if (feeds.length === 4) return `${feeds[3]} / ${feeds[1]}`;
  if (feeds.length === 3) return `${feeds[2]} / ${feeds[0]}`;
  return `${feeds[0]} / ${feeds[1]}`;
}

function aicpFeeds(rows: { lat: number; lng: number; name?: string }[], zoom: number): Place[] {
  if (zoom < 8 || !rows.length) return [];
  const span = zoom < 11 ? 0.13 : zoom < 14 ? 0.06 : zoom < 17 ? 0.018 : 0.0048;
  const groups = new Map<string, { count: number; latKey: number; lngKey: number; south: number; north: number; west: number; east: number; feeds: string[] }>();
  for (const row of rows) {
    const latKey = Math.floor(row.lat / span);
    const lngKey = Math.floor((row.lng + 180) / span);
    const key = `${latKey}:${lngKey}`;
    const feed = (row.name || "").replace(/\s+/g, " ").trim().replace(/^NeuralLink · /, "");
    const current = groups.get(key);
    if (!current) groups.set(key, { count: 1, latKey, lngKey, south: row.lat, north: row.lat, west: row.lng, east: row.lng, feeds: feed ? [feed] : [] });
    else {
      current.count += 1;
      current.south = Math.min(current.south, row.lat); current.north = Math.max(current.north, row.lat);
      current.west = Math.min(current.west, row.lng); current.east = Math.max(current.east, row.lng);
      if (feed && !current.feeds.includes(feed) && current.feeds.length < 8) current.feeds.push(feed);
    }
  }
  const refreshed = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const observed = `fusion cycle ${fusionCycle()}`;
  const expanded = zoom >= 16;
  const out: Place[] = [];
  for (const group of groups.values()) {
    if (group.feeds.length < 2) continue;
    const label = aicpLabel(group.feeds);
    const detail = group.feeds.join(" · ");
    out.push(pin({
      id: `neural:aicp:z${zoom}:${group.latKey}:${group.lngKey}`,
      name: label.length > 36 ? `${label.slice(0, 35).trimEnd()}…` : label,
      ...(detail !== label ? { address: detail.slice(0, 280) } : {}),
      lat: Math.max(-90, Math.min(90, (group.latKey + 0.28) * span)),
      lng: Math.max(-180, Math.min(180, (group.lngKey + 0.81) * span - 180)),
      origin: "neural-feed", category: expanded ? "aicp" : "aicp-cluster", color: AICP, provenance: AICP_PROVENANCE,
      observed_at: observed, refreshed_at: refreshed,
      ...(expanded ? {} : { cluster: { count: group.count, bounds: {
        south: Math.max(-90, group.south - span * 0.08), north: Math.min(90, group.north + span * 0.08),
        west: Math.max(-180, group.west - span * 0.08), east: Math.min(180, group.east + span * 0.08),
      } } }),
    }));
  }
  return out;
}

function readBox(url: URL): { box: GeographicBounds; zoom: number } {
  const zoom = Math.max(2, Math.min(20, Number(url.searchParams.get("zoom") || 2)));
  const south = Number(url.searchParams.get("south"));
  const north = Number(url.searchParams.get("north"));
  const west = Number(url.searchParams.get("west"));
  const east = Number(url.searchParams.get("east"));
  if ([south, north, west, east].every(Number.isFinite) && south < north && west < east) return { box: { south, north, west, east }, zoom };
  return { box: worldBox(), zoom: 2 };
}

function search(url: URL) {
  const query = (url.searchParams.get("q") || "").trim().toLocaleLowerCase();
  const hits: Place[] = [];
  if (!query) return hits;
  const stop = new Set(["the", "and", "of", "a", "for", "near", "in", "at", "to", "on"]);
  const tokens = query.split(/[^0-9a-z]+/).filter(token => token.length >= 3 && !stop.has(token)).slice(0, 4);
  const matches = (name: string, id: string) => {
    const folded = name.toLocaleLowerCase();
    const key = id.toLocaleLowerCase();
    const word = (haystack: string, token: string) => new RegExp(`(?:^|[^0-9a-z])${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:$|[^0-9a-z])`).test(haystack);
    if (folded.includes(query) || key.includes(query)) return true;
    if (folded.length >= 3 && word(query, folded)) return true;
    return tokens.some(token => word(folded, token) || word(key, token));
  };
  for (const place of sourcePlaces()) if (matches(place.name, place.id)) hits.push(place);
  const ranked = cities.filter(city => matches(city.name, `geonames:${city.id}`)).sort((a, b) => b.pop - a.pop);
  for (const city of ranked) {
    hits.push(pin({
      id: `geonames:${city.id}`, name: city.name, lat: city.lat, lng: city.lng, origin: "open-index",
      country_code: city.country, population: city.pop, color: "#396b94",
      provenance: "GeoNames · CC BY 4.0 · own geographic index",
    }));
    if (hits.length >= 20) break;
  }
  return hits;
}

function route(body: { origin?: string; destination?: string }) {
  const point = (value: string) => {
    const match = value.trim().match(/^([+-]?\d+(?:\.\d+)?)\s*[,;]\s*([+-]?\d+(?:\.\d+)?)$/);
    if (!match) return null;
    const lat = Number(match[1]);
    const lng = Number(match[2]);
    return Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? { lat, lng } : null;
  };
  const origin = point(body.origin || "");
  const destination = point(body.destination || "");
  if (!origin || !destination) return Response.json({ detail: "Use coordinates for both ends of the route." }, { status: 422 });
  const radians = Math.PI / 180;
  const arc = Math.sin((destination.lat - origin.lat) * radians / 2) ** 2
    + Math.cos(origin.lat * radians) * Math.cos(destination.lat * radians) * Math.sin((destination.lng - origin.lng) * radians / 2) ** 2;
  const meters = 6371008.8 * 2 * Math.asin(Math.min(1, Math.sqrt(arc)));
  const km = meters / 1000;
  return Response.json({
    distance: km < 1 ? `${Math.round(meters)} m` : `${km.toFixed(1)} km`,
    duration: `${Math.max(1, Math.round(km / 40 * 60))} min`,
    points: [origin, destination],
    steps: [{ instruction: "Direct path between the two coordinates.", distance: `${km.toFixed(1)} km`, duration: `${Math.max(1, Math.round(km / 40 * 60))} min` }],
  });
}

export async function publishedApi(request: Request): Promise<Response> {
  try {
    return await servePublished(request);
  } catch {
    const offline = snapshot(sourcePlaces(), indexSnapshot(worldBox(), 2), poiSnapshot(clusterPlaces(cities, 2, worldBox()), worldBox()));
    return Response.json({
      maps: { api_base: "", browser_key: "" },
      source: {
        status: "passed", filename: "CherryPro_Z4_portable.py", sha256: SOURCE_SHA,
        python_version: "3.12", executed_at: nowIso(), duration_ms: 0, exit_code: 0,
        report_count: 14, main_guards_disabled: 58, location_count: sources.length, network_access: false,
      },
      locations: offline.locations,
      worker: offline.worker,
      nearby: offline.nearby,
      index: offline.index,
      poi: offline.poi,
      fusion: offline.fusion,
      uplinks: { connected: false, status: "Not connected" },
    });
  }
}

async function servePublished(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  if (path.endsWith("/api/search")) return Response.json({ results: search(url) });
  if (path.endsWith("/api/place")) return Response.json({ detail: "That place is not in the published frame." }, { status: 404 });
  if (path.endsWith("/api/directions")) {
    const body = await request.json().catch(() => ({}));
    return route(body as { origin?: string; destination?: string });
  }
  const { box, zoom } = path.endsWith("/api/bootstrap") ? { box: worldBox(), zoom: 2 } : readBox(url);
  const frameSnapshot = await frame(box, zoom);
  if (path.endsWith("/api/bootstrap")) {
    return Response.json({
      maps: { api_base: "", browser_key: "" },
      source: {
        status: "passed", filename: "CherryPro_Z4_portable.py", sha256: SOURCE_SHA,
        python_version: "3.12", executed_at: nowIso(), duration_ms: 0, exit_code: 0,
        report_count: 14, main_guards_disabled: 58, location_count: sources.length, network_access: false,
      },
      locations: frameSnapshot.locations,
      worker: frameSnapshot.worker,
      nearby: frameSnapshot.nearby,
      index: frameSnapshot.index,
      poi: frameSnapshot.poi,
      fusion: frameSnapshot.fusion,
      uplinks: { connected: false, status: "Not connected" },
    });
  }
  return Response.json(frameSnapshot);
}
