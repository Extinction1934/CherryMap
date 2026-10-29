"""TPM-119 source-connected software bridge.

Owned by the CherryPro source process. Named places load off the snapshot
path. A close view that is still thin is filled in the background from the
same OpenStreetMap neighborhood read the source worker already uses.

Max zoom publishes three expanded layers from that proximity:
green software-bridge POIs on the real coordinates, purple NeuralLink
feeds shifted off those coordinates, green Restricted Lattice Partner Direct
feeds on tactical partner sites, and cyan Restricted Lattice Live
Intelligence is a separate blue lattice from the TPM-119 connector.
It folds every place in the proximity, with no category lock, and it does
not repeat NeuralLink's per-place pins. From far range it is coarse; closer
zoom splits it into finer nodes. Snapshots only read memory.
None of those channels copies another. From far-range zoom each channel
publishes its own clusters. Snapshots only read memory.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from array import array
from bisect import bisect_left, bisect_right
from urllib.parse import unquote, urlsplit

from .anaconda_plugin import pin_fields as anaconda_pin
from .iplasma_c3 import pin_fields as iplasma_pin
from .operator_terminal import pin_fields as operator_pin
from .portx_terminal import ORBITAL_ID, pin_fields as portx_pin

BRIDGE_COLOR = "#1e6b48"
NEURAL_COLOR = "#5c3d8f"
LATTICE_COLOR = BRIDGE_COLOR
LATTICE_PROVENANCE = "NeuralLink Restricted Lattice Partner Direct · TPM-119 source bridge"
LIVE_COLOR = "#1a4fbf"
LIVE_PROVENANCE = "TPM-119 connector · live tactical fusion"
MAX_CELLS = 140
CACHE_LIMIT = 48
FAR_RANGE_ZOOM = 8
EXPAND_ZOOM = 14
CLOSE_ZOOM = 16
PIN_CEILING = 500
PIN_CEILING_CLOSE = 1100
PIN_CEILING_MAX = 1400
TILE_SPAN = 0.008
FILL_TARGET = 36
_OSM_ID = re.compile(r"osm:(?:node|way|relation):[1-9][0-9]*\Z")
_SKIP_CATEGORIES = {
    "street_lamp", "traffic_signals", "street_cabinet", "surveillance", "camera",
    "manhole", "survey_point", "monitoring_station", "milestone", "chimney",
    "flagpole", "traffic_mirror",
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "track", "path", "footway",
    "cycleway", "pedestrian", "road", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
    "platform", "stop_position", "halt", "bus_stop", "tram_stop", "rail",
    "waste_basket", "bicycle_parking", "vending_machine", "post_box", "bench", "recycling",
}
# Partner Direct is a tactical channel. It is not every named place NeuralLink already feeds.
_PARTNER_DIRECT = frozenset({
    "government", "diplomatic", "embassy", "consulate", "townhall", "courthouse",
    "police", "fire_station", "hospital", "prison", "military", "barracks",
    "checkpoint", "border_control", "customs", "coast_guard", "rescue_station",
    "ambulance_station", "ranger_station", "aerodrome", "helipad", "ferry_terminal",
    "harbour", "port", "marina", "university", "college", "research_institute",
    "ngo", "ministry", "parliament", "civic", "public_building",
})
# Live Intelligence follows the bridge into every proximity. Civic places stay off it.
_LIVE_EXCLUDED = frozenset({
    "library", "park", "garden", "playground", "nature_reserve", "dog_park",
    "pharmacy", "chemist", "museum", "school", "kindergarten", "college", "university",
    "language_school", "music_school", "driving_school", "educational_institution",
    "clinic", "doctors", "dentist", "hospital", "social_facility", "community_centre",
    "place_of_worship", "post_office", "memorial", "artwork", "attraction", "information",
    "public_building", "civic", "townhall", "courthouse", "government", "diplomatic",
    "embassy", "consulate", "police", "fire_station", "prison", "military", "ngo",
    "research_institute", "ministry", "parliament",
})


def _log(message: str) -> None:
    try:
        with open("/tmp/tpm119-source-bridge.log", "a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        return


def _clean(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or any(ord(character) < 32 for character in cleaned):
        return None
    return cleaned


def _connect():
    raw = os.getenv("DATABASE_URL", "")
    parsed = urlsplit(raw)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not database:
        raise RuntimeError("index unavailable")
    import pymysql

    return pymysql.connect(
        host=parsed.hostname,
        port=int(parsed.port or 3306),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=120,
    )


class _LatIndex:
    __slots__ = ("lats", "lngs", "names", "tokens", "kinds", "categories")

    def __init__(self) -> None:
        self.lats: array = array("d")
        self.lngs: array = array("d")
        self.names: list[str] = []
        self.tokens: list[str] = []
        self.kinds: bytearray = bytearray()
        self.categories: list[str] = []

    def __len__(self) -> int:
        return len(self.lats)


def _pack(rows: list[tuple[float, float, str, str, int, str]]) -> _LatIndex:
    rows.sort(key=lambda item: item[0])
    index = _LatIndex()
    for lat, lng, name, token, kind, category in rows:
        index.lats.append(lat)
        index.lngs.append(lng)
        index.names.append(name)
        index.tokens.append(token)
        index.kinds.append(kind)
        index.categories.append(category)
    return index


def _cell_size(zoom: int, south: float, north: float, west: float, east: float) -> float:
    span_lat = max(north - south, 1e-8)
    span_lng = max(east - west, 1e-8)
    cell = max(360.0 / float(2 ** (max(0, min(22, zoom)) + 3)), 1e-5)
    for _ in range(32):
        nlat = max(1, math.ceil(span_lat / cell - 1e-12))
        nlng = max(1, math.ceil(span_lng / cell - 1e-12))
        if nlat * nlng <= MAX_CELLS:
            return cell
        cell *= 1.45
    return max(span_lat, span_lng)


def _marker(name: str, color: str, provenance: str) -> dict[str, object]:
    return {
        "title": name,
        "label": name,
        "color": color,
        "popup": {"heading": name, "provenance": provenance},
    }


def _cluster_record(
    *,
    identifier: str,
    name: str,
    lat: float,
    lng: float,
    origin: str,
    category: str,
    color: str,
    provenance: str,
    count: int,
    bounds: dict[str, float],
) -> dict[str, object]:
    return {
        "id": identifier,
        "name": name,
        "lat": lat,
        "lng": lng,
        "origin": origin,
        "category": category,
        "marker": _marker(name, color, provenance),
        "cluster": {"count": count, "bounds": bounds},
    }


def _pin_record(
    *,
    identifier: str,
    name: str,
    lat: float,
    lng: float,
    origin: str,
    category: str,
    color: str,
    provenance: str,
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": identifier,
        "name": name,
        "lat": lat,
        "lng": lng,
        "origin": origin,
        "marker": _marker(name, color, provenance),
    }
    if category:
        record["category"] = category
    return record


def _bounds(south: float, north: float, west: float, east: float, pad: float) -> dict[str, float]:
    south_b = max(-90.0, south - pad)
    north_b = min(90.0, north + pad)
    west_b = max(-180.0, west - pad)
    east_b = min(180.0, east + pad)
    if north_b <= south_b:
        north_b = min(90.0, south_b + 0.004)
    if east_b <= west_b:
        east_b = min(180.0, west_b + 0.004)
    return {"south": south_b, "north": north_b, "west": west_b, "east": east_b}


def _tiles(south: float, north: float, west: float, east: float) -> list[tuple[float, float, float, float]]:
    """Up to four 0.008° neighborhoods, the one under the camera first."""
    center_lat = (south + north) * 0.5
    center_lng = (west + east) * 0.5
    lat_i = math.floor((center_lat + 90.0) / TILE_SPAN)
    lng_i = math.floor((center_lng + 180.0) / TILE_SPAN)
    tiles: list[tuple[float, float, float, float]] = []
    for dlat, dlng in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        low_lat = max(-90.0, -90.0 + (lat_i + dlat) * TILE_SPAN)
        high_lat = min(90.0, low_lat + TILE_SPAN)
        low_lng = max(-180.0, -180.0 + (lng_i + dlng) * TILE_SPAN)
        high_lng = min(180.0, low_lng + TILE_SPAN)
        if low_lat < high_lat and low_lng < high_lng:
            tiles.append((round(low_lat, 6), round(high_lat, 6), round(low_lng, 6), round(high_lng, 6)))
    return tiles[:4]


def _feed_point(
    lat: float, lng: float, south: float, north: float, west: float, east: float, zoom: int,
) -> tuple[float, float]:
    """Place a NeuralLink feed beside its POI, still inside the block."""
    shift = 0.00115 if zoom <= 16 else 0.00058 if zoom <= 17 else 0.00032
    candidates = (
        (lat + shift, lng + shift * 0.7),
        (lat - shift, lng - shift * 0.7),
        (lat + shift * 0.55, lng - shift * 0.25),
        (lat - shift * 0.4, lng + shift * 0.55),
    )
    for raw_lat, raw_lng in candidates:
        next_lat = max(-90.0, min(90.0, raw_lat))
        next_lng = max(-180.0, min(180.0, raw_lng))
        if south <= next_lat <= north and west <= next_lng <= east:
            if abs(next_lat - lat) > 1e-7 or abs(next_lng - lng) > 1e-7:
                return next_lat, next_lng
    inward_lat = min(north, max(south, lat + shift * 0.35))
    if abs(inward_lat - lat) < 1e-7:
        inward_lat = min(north, lat + max((north - south) * 0.08, 1e-5))
    return max(-90.0, min(90.0, inward_lat)), lng


def _lattice_point(
    lat: float,
    lng: float,
    feed_lat: float,
    feed_lng: float,
    south: float,
    north: float,
    west: float,
    east: float,
    zoom: int,
) -> tuple[float, float]:
    """Place a Partner Direct feed off the site, clear of the NeuralLink pin."""
    shift = 0.0024 if zoom <= 16 else 0.0012 if zoom <= 17 else 0.00064
    candidates = (
        (lat + shift, lng - shift * 0.72),
        (lat - shift, lng + shift * 0.72),
        (lat + shift * 0.2, lng - shift),
        (lat - shift * 0.85, lng - shift * 0.2),
    )

    def apart(a: float, b: float, c: float, d: float) -> bool:
        return abs(a - c) > 1e-7 or abs(b - d) > 1e-7

    for raw_lat, raw_lng in candidates:
        next_lat = max(-90.0, min(90.0, raw_lat))
        next_lng = max(-180.0, min(180.0, raw_lng))
        if not (south <= next_lat <= north and west <= next_lng <= east):
            continue
        if apart(next_lat, next_lng, lat, lng) and apart(next_lat, next_lng, feed_lat, feed_lng):
            return next_lat, next_lng
    inward_lat = max(south, min(north, lat - shift * 0.4))
    inward_lng = max(west, min(east, lng - shift * 0.25))
    if not apart(inward_lat, inward_lng, lat, lng) or not apart(inward_lat, inward_lng, feed_lat, feed_lng):
        inward_lat = max(south, lat - max((north - south) * 0.08, 1e-5))
        inward_lng = max(west, lng - max((east - west) * 0.06, 1e-5))
    return max(-90.0, min(90.0, inward_lat)), max(-180.0, min(180.0, inward_lng))


def _partner_direct(category: str) -> bool:
    return category in _PARTNER_DIRECT


def _lattice_name(name: str, category: str) -> str:
    role = category.replace("_", " ")
    joined = f"Partner Direct · {role} · {name}"
    if len(joined) <= 180:
        return joined
    return joined[:179].rstrip() + "…"


def _live_intelligence(category: str) -> bool:
    return category not in _PARTNER_DIRECT and category not in _LIVE_EXCLUDED


def _live_name(name: str, category: str) -> str:
    if category in {"yes", "mapped feature", "populated-place"}:
        joined = f"Live Intelligence · {name}"
    else:
        joined = f"Live Intelligence · {category.replace('_', ' ')} · {name}"
    if len(joined) <= 180:
        return joined
    return joined[:179].rstrip() + "…"


def _live_point(
    lat: float,
    lng: float,
    occupied: list[tuple[float, float]],
    south: float,
    north: float,
    west: float,
    east: float,
    zoom: int,
) -> tuple[float, float]:
    """Place a Live Intelligence feed east of the site, clear of every other pin."""
    shift = 0.0018 if zoom <= 16 else 0.0009 if zoom <= 17 else 0.00048
    candidates = (
        (lat, lng + shift),
        (lat - shift, lng),
        (lat - shift * 0.65, lng + shift * 0.65),
        (lat + shift * 0.15, lng + shift * 0.9),
    )

    def apart(a: float, b: float, c: float, d: float) -> bool:
        return abs(a - c) > 1e-7 or abs(b - d) > 1e-7

    for raw_lat, raw_lng in candidates:
        next_lat = max(-90.0, min(90.0, raw_lat))
        next_lng = max(-180.0, min(180.0, raw_lng))
        if not (south <= next_lat <= north and west <= next_lng <= east):
            continue
        if all(apart(next_lat, next_lng, other_lat, other_lng) for other_lat, other_lng in occupied):
            return next_lat, next_lng
    inward_lat = max(south, min(north, lat - shift * 0.35))
    inward_lng = min(east, max(west, lng + shift * 0.55))
    return max(-90.0, min(90.0, inward_lat)), max(-180.0, min(180.0, inward_lng))


def _remember_feed(agg: list, name: object) -> None:
    text = " ".join(str(name or "").split())
    if not text:
        return
    for slot in agg:
        if isinstance(slot, list):
            if text not in slot and len(slot) < 24:
                slot.append(text)
            return


def _fusion_names(payload: list) -> list[str]:
    raw: list[str] = []
    if payload and isinstance(payload[0], tuple):
        raw = [str(item[4] or "") for item in payload if len(item) > 4]
    else:
        stored = payload[21] if len(payload) > 21 and isinstance(payload[21], list) else []
        raw = [str(item) for item in stored]
    seen: list[str] = []
    for item in raw:
        name = " ".join(item.split())
        if name.startswith("NeuralLink · "):
            name = name.split(" · ", 1)[1].strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= 8:
            break
    return seen


def _fusion_point(payload: list, center_lat: float, center_lng: float, cell: float) -> tuple[float, float]:
    if payload and isinstance(payload[0], tuple):
        lat = sum(float(item[0]) for item in payload) / len(payload)
        lng = sum(float(item[1]) for item in payload) / len(payload)
    else:
        count = max(1, int(payload[0]))
        lat = float(payload[1]) / count
        lng = float(payload[2]) / count
    shift = max(cell * 0.35, 0.004)
    if lat >= center_lat:
        lat -= shift
    else:
        lat += shift
    if lng >= center_lng:
        lng -= shift
    else:
        lng += shift
    return max(-90.0, min(90.0, lat)), max(-180.0, min(180.0, lng))


def _touch_partner(agg: list, lat: float, lng: float, category: str) -> None:
    if not _partner_direct(category):
        return
    if agg[7] == 0:
        agg[7] = 1
        agg[8] = lat
        agg[9] = lng
        agg[10] = lat
        agg[11] = lat
        agg[12] = lng
        agg[13] = lng
        return
    agg[7] += 1
    agg[8] += lat
    agg[9] += lng
    if lat < agg[10]:
        agg[10] = lat
    if lat > agg[11]:
        agg[11] = lat
    if lng < agg[12]:
        agg[12] = lng
    if lng > agg[13]:
        agg[13] = lng


def _touch_live(agg: list, lat: float, lng: float, category: str) -> None:
    if not _live_intelligence(category):
        return
    if agg[14] == 0:
        agg[14] = 1
        agg[15] = lat
        agg[16] = lng
        agg[17] = lat
        agg[18] = lat
        agg[19] = lng
        agg[20] = lng
        return
    agg[14] += 1
    agg[15] += lat
    agg[16] += lng
    if lat < agg[17]:
        agg[17] = lat
    if lat > agg[18]:
        agg[18] = lat
    if lng < agg[19]:
        agg[19] = lng
    if lng > agg[20]:
        agg[20] = lng


class Tpm119SourceBridge:
    """Background load plus in-memory frames. The snapshot path never waits on I/O."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._geo = _LatIndex()
        self._osm = _LatIndex()
        self._live = _LatIndex()
        self._live_rows: dict[str, tuple[float, float, str, str, int, str]] = {}
        self._osm_token: tuple[object, ...] = ()
        self._gen = 0
        self._cache: dict[tuple, list[dict[str, object]]] = {}
        self._started = False
        self._stop = False
        self._demand: tuple[float, float, float, float, int, float] | None = None
        self._tried: dict[tuple[float, float, float, float], float] = {}
        self._boot = time.monotonic()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="tpm119-source-bridge", daemon=True).start()

    def _run(self) -> None:
        try:
            self._load_geo()
        except Exception as exc:
            _log(f"geo-load-failed {type(exc).__name__}")
        while not self._stop:
            try:
                self._load_osm()
            except Exception as exc:
                _log(f"osm-load-failed {type(exc).__name__}")
            try:
                self._fill_demand()
            except Exception as exc:
                _log(f"fill-failed {type(exc).__name__}")
            time.sleep(1.2)

    def _load_geo(self) -> None:
        connection = _connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT latitude, longitude, geoname_id, name FROM geo_places")
                fetched = cursor.fetchall()
        finally:
            connection.close()
        rows: list[tuple[float, float, str, str, int, str]] = []
        for lat, lng, gid, name in fetched:
            clean = _clean(str(name), 255)
            try:
                latitude, longitude, identifier = float(lat), float(lng), int(gid)
            except (TypeError, ValueError):
                continue
            if clean is None or not math.isfinite(latitude) or not math.isfinite(longitude):
                continue
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0 or identifier <= 0:
                continue
            rows.append((latitude, longitude, clean, str(identifier), ord("g"), "populated-place"))
        packed = _pack(rows)
        with self._lock:
            self._geo = packed
            self._gen += 1
            self._cache.clear()
        _log(f"geo-ready {len(packed)}")

    def _load_osm(self) -> None:
        connection = _connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT osm_key, name, latitude, longitude, category FROM open_poi_places")
                fetched = cursor.fetchall()
        finally:
            connection.close()
        rows: list[tuple[float, float, str, str, int, str]] = []
        for key, name, lat, lng, category in fetched:
            identifier = str(key)
            if _OSM_ID.fullmatch(identifier) is None:
                continue
            clean = _clean(str(name), 255)
            kind = _clean(str(category or ""), 64) or "mapped feature"
            if kind in _SKIP_CATEGORIES or clean is None:
                continue
            try:
                latitude, longitude = float(lat), float(lng)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(latitude) or not math.isfinite(longitude):
                continue
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
                continue
            rows.append((latitude, longitude, clean, identifier, ord("o"), kind))
        token = (len(rows), round(sum(item[0] for item in rows), 4), round(sum(item[1] for item in rows), 4))
        with self._lock:
            if token == self._osm_token:
                return
            self._osm = _pack(rows)
            self._osm_token = token
            self._gen += 1
            self._cache.clear()
        _log(f"osm-ready {len(rows)}")

    def _fill_demand(self) -> None:
        with self._lock:
            demand = self._demand
        if demand is None:
            return
        south, north, west, east, zoom, noted = demand
        if zoom < 12 or time.monotonic() - noted > 25.0:
            return
        if self._hits(south, north, west, east) >= FILL_TARGET:
            return
        now = time.monotonic()
        for tile in _tiles(south, north, west, east):
            if self._tried.get(tile, 0.0) > now:
                continue
            self._fetch_tile(tile)
            return

    def _fetch_tile(self, tile: tuple[float, float, float, float]) -> None:
        south, north, west, east = tile
        try:
            from .poi_runtime import PoiBounds, fetch_osm_map

            records = fetch_osm_map(PoiBounds(
                south=south, north=north, west=west, east=east, zoom=16,
            ))
        except Exception as exc:
            self._tried[tile] = time.monotonic() + 25.0
            _log(f"tile-failed {tile[0]:.3f},{tile[1]:.3f} {type(exc).__name__}")
            return
        added = self._ingest(records)
        self._tried[tile] = time.monotonic() + (3600.0 if added else 600.0)
        _log(f"tile-ready {south:.3f},{west:.3f} added {added}")

    def _ingest(self, records: list[dict[str, object]]) -> int:
        added = 0
        with self._lock:
            for record in records:
                identifier = str(record.get("id") or "")
                if _OSM_ID.fullmatch(identifier) is None or identifier in self._live_rows:
                    continue
                name = _clean(record.get("name"), 255)
                category = _clean(str(record.get("category") or ""), 64) or "mapped feature"
                if name is None or category in _SKIP_CATEGORIES:
                    continue
                try:
                    lat, lng = float(record["lat"]), float(record["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(lat) or not math.isfinite(lng):
                    continue
                if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
                    continue
                self._live_rows[identifier] = (lat, lng, name, identifier, ord("o"), category)
                added += 1
            if added:
                self._live = _pack(list(self._live_rows.values()))
                self._gen += 1
                self._cache.clear()
        return added

    def _hits(self, south: float, north: float, west: float, east: float) -> int:
        with self._lock:
            indexes = (self._osm, self._live)
        total = 0
        for index in indexes:
            lats = index.lats
            if not lats:
                continue
            start, stop = bisect_left(lats, south), bisect_right(lats, north)
            lngs = index.lngs
            for offset in range(start, stop):
                if west <= lngs[offset] <= east:
                    total += 1
        return total

    def frame(self, *, south: float, north: float, west: float, east: float, zoom: int) -> list[dict[str, object]]:
        zoom = max(2, min(20, int(zoom)))
        if zoom >= 12 and south < north and west <= east:
            with self._lock:
                self._demand = (south, north, west, east, zoom, time.monotonic())
        cycle = self._fusion_cycle()
        key = (zoom, round(south, 5), round(north, 5), round(west, 5), round(east, 5), cycle)
        with self._lock:
            geo, osm, live, gen = self._geo, self._osm, self._live, self._gen
            cached = self._cache.get((gen, *key))
        if cached is not None:
            return cached
        if west > east:
            built = self._build(geo, osm, live, south, north, west, 180.0, zoom)
            built.extend(self._build(geo, osm, live, south, north, -180.0, east, zoom))
        else:
            built = self._build(geo, osm, live, south, north, west, east, zoom)
        with self._lock:
            if self._gen == gen:
                if len(self._cache) >= CACHE_LIMIT:
                    self._cache.clear()
                self._cache[(gen, *key)] = built
        return built

    def _fusion_cycle(self) -> int:
        return max(1, int(time.monotonic() - self._boot) + 1)

    def _build(
        self,
        geo: _LatIndex,
        osm: _LatIndex,
        live: _LatIndex,
        south: float,
        north: float,
        west: float,
        east: float,
        zoom: int,
    ) -> list[dict[str, object]]:
        if south >= north or west == east:
            return []
        cell = _cell_size(zoom, south, north, west, east)
        groups: dict[tuple[int, int], list] = {}
        seen: set[str] = set()
        expand = zoom >= EXPAND_ZOOM
        for index in (live, osm, geo):
            self._accumulate(index, south, north, west, east, cell, groups, expand, seen)
        if not groups:
            return []
        places: list[dict[str, object]] = []
        if expand:
            self._emit_expanded(groups, zoom, places, south, north, west, east)
        else:
            self._emit_clusters(groups, zoom, cell, places)
        self._emit_live_lattice(groups, zoom, cell, places)
        self._emit_portx(groups, zoom, places)
        self._emit_operator(groups, zoom, places)
        self._emit_fusion_context(groups, zoom, cell, places, south, north, west, east)
        self._emit_iplasma(groups, zoom, places)
        return places

    @staticmethod
    def _accumulate(
        index: _LatIndex,
        south: float,
        north: float,
        west: float,
        east: float,
        cell: float,
        groups: dict,
        keep_members: bool,
        seen: set[str],
    ) -> None:
        lats = index.lats
        if not lats:
            return
        start, stop = bisect_left(lats, south), bisect_right(lats, north)
        lngs = index.lngs
        inverse = 1.0 / cell
        for offset in range(start, stop):
            lng = lngs[offset]
            if lng < west or lng > east:
                continue
            token = index.tokens[offset]
            if token in seen:
                continue
            seen.add(token)
            lat = lats[offset]
            key = (math.floor(lat * inverse), math.floor((lng + 180.0) * inverse))
            if keep_members:
                bucket = groups.get(key)
                if bucket is None:
                    bucket = []
                    groups[key] = bucket
                bucket.append((lat, lng, index.kinds[offset], token, index.names[offset], index.categories[offset]))
                continue
            current = groups.get(key)
            category = index.categories[offset]
            if current is None:
                groups[key] = [1, lat, lng, lat, lat, lng, lng, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, []]
                _remember_feed(groups[key], index.names[offset])
                _touch_partner(groups[key], lat, lng, category)
                _touch_live(groups[key], lat, lng, category)
                continue
            current[0] += 1
            current[1] += lat
            current[2] += lng
            if lat < current[3]:
                current[3] = lat
            if lat > current[4]:
                current[4] = lat
            if lng < current[5]:
                current[5] = lng
            if lng > current[6]:
                current[6] = lng
            _remember_feed(current, index.names[offset])
            _touch_partner(current, lat, lng, category)
            _touch_live(current, lat, lng, category)

    def _emit_clusters(self, groups: dict, zoom: int, cell: float, places: list[dict[str, object]]) -> None:
        pad = max(cell * 0.08, 0.002)
        for (lat_i, lng_i), agg in groups.items():
            count = int(agg[0])
            if count < 1 or count > 236_560:
                continue
            mean_lat = max(-90.0, min(90.0, agg[1] / count))
            mean_lng = max(-180.0, min(180.0, agg[2] / count))
            bounds = _bounds(agg[3], agg[4], agg[5], agg[6], pad)
            # NeuralLink sits on the far corner of the cell so it cannot cover the bridge cluster.
            neural_lat = bounds["north"]
            neural_lng = bounds["east"]
            if abs(neural_lat - mean_lat) < 1e-5 and abs(neural_lng - mean_lng) < 1e-5:
                neural_lat = min(90.0, mean_lat + max(pad, 0.02))
                neural_lng = min(180.0, mean_lng + max(pad, 0.02))
            places.append(_cluster_record(
                identifier=f"poicluster:z{zoom}:{lat_i}:{lng_i}",
                name=f"{count} bridge places", lat=mean_lat, lng=mean_lng, origin="open-poi",
                category="poi-cluster", color=BRIDGE_COLOR,
                provenance="TPM-119 source bridge · zoom fusion",
                count=count, bounds=bounds,
            ))
            places.append(_cluster_record(
                identifier=f"neural:z{zoom}:{lat_i}:{lng_i}",
                name=f"NeuralLink · {count} feeds", lat=neural_lat, lng=neural_lng, origin="neural-feed",
                category="neural-cluster", color=NEURAL_COLOR,
                provenance="NeuralLink feeds · TPM-119 source bridge",
                count=count, bounds=bounds,
            ))
            partner = int(agg[7]) if len(agg) > 13 else 0
            lattice_lat = mean_lat
            lattice_lng = mean_lng
            if zoom >= FAR_RANGE_ZOOM and len(agg) >= 14 and 1 <= partner <= 236_560:
                lattice_lat = max(-90.0, min(90.0, agg[8] / partner))
                lattice_lng = max(-180.0, min(180.0, agg[9] / partner))
                stacked = (
                    abs(lattice_lat - mean_lat) < 1e-5 and abs(lattice_lng - mean_lng) < 1e-5
                ) or (
                    abs(lattice_lat - neural_lat) < 1e-5 and abs(lattice_lng - neural_lng) < 1e-5
                )
                if stacked:
                    lattice_lat = max(-90.0, mean_lat - max(pad, 0.02))
                    lattice_lng = max(-180.0, mean_lng - max(pad, 0.02))
                places.append(_cluster_record(
                    identifier=f"neural:z{zoom}:lattice:{lat_i}:{lng_i}",
                    name=f"Partner Direct · {partner} tactical", lat=lattice_lat, lng=lattice_lng, origin="neural-feed",
                    category="lattice-cluster", color=LATTICE_COLOR,
                    provenance=LATTICE_PROVENANCE,
                    count=partner, bounds=bounds,
                ))

    def _emit_live_lattice(self, groups: dict, zoom: int, cell: float, places: list[dict[str, object]]) -> None:
        """One lattice feed per proximity. The title is that cell's own records."""
        del cell
        if zoom < 8 or not groups:
            return
        cycle = self._fusion_cycle()
        refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        span = 0.2 if zoom < 11 else 0.08 if zoom < EXPAND_ZOOM else 0.004 if zoom < 17 else 0.0015
        buckets: dict[tuple[int, int], list] = {}
        for payload in groups.values():
            points: list[tuple[float, float, list[str]]] = []
            if payload and isinstance(payload[0], tuple):
                points = [(float(item[0]), float(item[1]), [str(item[4] or "")]) for item in payload]
            else:
                count = int(payload[0]) if payload else 0
                if 1 <= count <= 236_560:
                    stored = payload[21] if len(payload) > 21 and isinstance(payload[21], list) else []
                    points = [(float(payload[1]) / count, float(payload[2]) / count, [str(item) for item in stored])]
            for lat, lng, names in points:
                key = (math.floor(lat / span), math.floor((lng + 180.0) / span))
                current = buckets.get(key)
                if current is None:
                    current = [1, lat, lng, lat, lat, lng, lng, []]
                    buckets[key] = current
                else:
                    current[0] += 1
                    current[1] += lat
                    current[2] += lng
                    if lat < current[3]:
                        current[3] = lat
                    if lat > current[4]:
                        current[4] = lat
                    if lng < current[5]:
                        current[5] = lng
                    if lng > current[6]:
                        current[6] = lng
                for name in names:
                    _remember_feed(current, name)
        expanded = zoom >= EXPAND_ZOOM
        for (lat_i, lng_i), agg in buckets.items():
            count = int(agg[0])
            if count < 1:
                continue
            fields = anaconda_pin(cycle, list(agg[7]))
            if not fields["name"]:
                continue
            lat = max(-90.0, min(90.0, agg[1] / count))
            lng = max(-180.0, min(180.0, agg[2] / count))
            if expanded:
                record = _pin_record(
                    identifier=f"neural:z{zoom}:anaconda:{lat_i}:{lng_i}",
                    name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category=fields["category"], color=fields["color"], provenance=fields["provenance"],
                )
            else:
                record = _cluster_record(
                    identifier=f"neural:z{zoom}:anaconda:{lat_i}:{lng_i}",
                    name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category="anaconda-cluster", color=fields["color"], provenance=fields["provenance"],
                    count=count, bounds=_bounds(agg[3], agg[4], agg[5], agg[6], span * 0.08),
                )
            if fields["address"]:
                record["address"] = fields["address"]
            record["observed_at"] = fields["observed_at"]
            record["refreshed_at"] = refreshed
            places.append(record)

    def _emit_portx(self, groups: dict, zoom: int, places: list[dict[str, object]]) -> None:
        """Every PortX cell keeps the records fused into it. No shared title, no cap."""
        if zoom < FAR_RANGE_ZOOM or not groups:
            return
        cycle = self._fusion_cycle()
        refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        span = 0.08 if zoom < 11 else 0.03 if zoom < 14 else 0.012 if zoom < 17 else 0.004
        buckets: dict[tuple[int, int], list] = {}
        for payload in groups.values():
            points: list[tuple[float, float, list[str]]] = []
            if payload and isinstance(payload[0], tuple):
                points = [(float(item[0]), float(item[1]), [str(item[4] or "")]) for item in payload]
            else:
                count = int(payload[0]) if payload else 0
                if 1 <= count <= 236_560:
                    stored = payload[21] if len(payload) > 21 and isinstance(payload[21], list) else []
                    points = [(float(payload[1]) / count, float(payload[2]) / count, [str(item) for item in stored])]
            for lat, lng, names in points:
                key = (math.floor(lat / span), math.floor((lng + 180.0) / span))
                current = buckets.get(key)
                if current is None:
                    current = [1, lat, lng, lat, lat, lng, lng, []]
                    buckets[key] = current
                else:
                    current[0] += 1
                    current[1] += lat
                    current[2] += lng
                    if lat < current[3]:
                        current[3] = lat
                    if lat > current[4]:
                        current[4] = lat
                    if lng < current[5]:
                        current[5] = lng
                    if lng > current[6]:
                        current[6] = lng
                for name in names:
                    _remember_feed(current, name)
        expanded = zoom >= 17
        for (lat_i, lng_i), agg in buckets.items():
            count = int(agg[0])
            if count < 1:
                continue
            lat = max(-90.0, min(90.0, (lat_i + 0.62) * span))
            lng = max(-180.0, min(180.0, (lng_i + 0.38) * span - 180.0))
            fields = portx_pin(cycle, list(agg[7]))
            if not fields["name"]:
                continue
            record_id = f"neural:portx:{ORBITAL_ID}:z{zoom}:{lat_i}:{lng_i}"
            if expanded:
                record = _pin_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category=fields["category"], color=fields["color"], provenance=fields["provenance"],
                )
            else:
                record = _cluster_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category="portx-cluster", color=fields["color"], provenance=fields["provenance"],
                    count=count, bounds=_bounds(agg[3], agg[4], agg[5], agg[6], span * 0.08),
                )
            if fields["address"]:
                record["address"] = fields["address"]
            record["observed_at"] = fields["observed_at"]
            record["refreshed_at"] = refreshed
            places.append(record)

    def _emit_operator(self, groups: dict, zoom: int, places: list[dict[str, object]]) -> None:
        """Operator cells use their own span and the trailing record, not another channel's pin."""
        if zoom < FAR_RANGE_ZOOM or not groups:
            return
        cycle = self._fusion_cycle()
        refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        span = 0.14 if zoom < 11 else 0.045 if zoom < 14 else 0.009 if zoom < 17 else 0.0028
        buckets: dict[tuple[int, int], list] = {}
        for payload in groups.values():
            points: list[tuple[float, float, list[str]]] = []
            if payload and isinstance(payload[0], tuple):
                points = [(float(item[0]), float(item[1]), [str(item[4] or "")]) for item in payload]
            else:
                count = int(payload[0]) if payload else 0
                if 1 <= count <= 236_560:
                    stored = payload[21] if len(payload) > 21 and isinstance(payload[21], list) else []
                    points = [(float(payload[1]) / count, float(payload[2]) / count, [str(item) for item in stored])]
            for lat, lng, names in points:
                key = (math.floor(lat / span), math.floor((lng + 180.0) / span))
                current = buckets.get(key)
                if current is None:
                    current = [1, lat, lng, lat, lat, lng, lng, []]
                    buckets[key] = current
                else:
                    current[0] += 1
                    current[1] += lat
                    current[2] += lng
                    if lat < current[3]:
                        current[3] = lat
                    if lat > current[4]:
                        current[4] = lat
                    if lng < current[5]:
                        current[5] = lng
                    if lng > current[6]:
                        current[6] = lng
                for name in names:
                    _remember_feed(current, name)
        expanded = zoom >= 16
        for (lat_i, lng_i), agg in buckets.items():
            count = int(agg[0])
            if count < 1:
                continue
            lat = max(-90.0, min(90.0, (lat_i + 0.18) * span))
            lng = max(-180.0, min(180.0, (lng_i + 0.81) * span - 180.0))
            fields = operator_pin(cycle, list(agg[7]))
            if not fields["name"]:
                continue
            record_id = f"neural:operator:z{zoom}:{lat_i}:{lng_i}"
            if expanded:
                record = _pin_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category=fields["category"], color=fields["color"], provenance=fields["provenance"],
                )
            else:
                record = _cluster_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category="operator-cluster", color=fields["color"], provenance=fields["provenance"],
                    count=count, bounds=_bounds(agg[3], agg[4], agg[5], agg[6], span * 0.08),
                )
            if fields["address"]:
                record["address"] = fields["address"]
            record["observed_at"] = fields["observed_at"]
            record["refreshed_at"] = refreshed
            places.append(record)

    def _emit_fusion_context(
        self,
        groups: dict,
        zoom: int,
        cell: float,
        places: list[dict[str, object]],
        south: float,
        north: float,
        west: float,
        east: float,
    ) -> None:
        """One fused-context readout per spread cell. The text is the records in that cell."""
        if zoom < FAR_RANGE_ZOOM:
            return
        cycle = self._fusion_cycle()
        refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        center_lat = (south + north) * 0.5
        center_lng = (west + east) * 0.5
        ranked: list[tuple[int, tuple[int, int], list]] = []
        for key, payload in groups.items():
            names = _fusion_names(payload)
            if len(names) < 2:
                continue
            ranked.append((len(names), key, payload))
        ranked.sort(key=lambda item: item[0], reverse=True)
        chosen: list[tuple[float, float]] = []
        gap = max(cell * 2.5, 0.01)
        for _, (lat_i, lng_i), payload in ranked:
            names = _fusion_names(payload)
            lat, lng = _fusion_point(payload, center_lat, center_lng, cell)
            if any(abs(lat - other_lat) < gap and abs(lng - other_lng) < gap for other_lat, other_lng in chosen):
                continue
            label = " · ".join(names[:3])
            if len(label) > 52:
                label = label[:51].rstrip() + "…"
            detail = " · ".join(names)
            if len(detail) > 280:
                detail = detail[:279].rstrip() + "…"
            record = _pin_record(
                identifier=f"neural:interconnect:z{zoom}:{lat_i}:{lng_i}",
                name=label, lat=lat, lng=lng, origin="neural-feed",
                category="interconnect-fusion", color="#0f766e",
                provenance="Interconnect 2.0 · TPM-119 software bridge",
            )
            if detail != label:
                record["address"] = detail
            record["observed_at"] = f"fusion cycle {cycle}"
            record["refreshed_at"] = refreshed
            places.append(record)
            chosen.append((lat, lng))
            if len(chosen) >= 8:
                break

    def _emit_iplasma(self, groups: dict, zoom: int, places: list[dict[str, object]]) -> None:
        """One iPlasma C3 pin per fusion cell. Own grid, own readout, same diaphragm."""
        if zoom < FAR_RANGE_ZOOM or not groups:
            return
        cycle = self._fusion_cycle()
        refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        span = 0.11 if zoom < 11 else 0.055 if zoom < 14 else 0.016 if zoom < 17 else 0.0055
        buckets: dict[tuple[int, int], list] = {}
        for payload in groups.values():
            points: list[tuple[float, float, list[str]]] = []
            if payload and isinstance(payload[0], tuple):
                points = [(float(item[0]), float(item[1]), [str(item[4] or "")]) for item in payload]
            else:
                count = int(payload[0]) if payload else 0
                if 1 <= count <= 236_560:
                    stored = payload[21] if len(payload) > 21 and isinstance(payload[21], list) else []
                    points = [(float(payload[1]) / count, float(payload[2]) / count, [str(item) for item in stored])]
            for lat, lng, names in points:
                key = (math.floor(lat / span), math.floor((lng + 180.0) / span))
                current = buckets.get(key)
                if current is None:
                    current = [1, lat, lng, lat, lat, lng, lng, []]
                    buckets[key] = current
                else:
                    current[0] += 1
                    current[1] += lat
                    current[2] += lng
                    if lat < current[3]:
                        current[3] = lat
                    if lat > current[4]:
                        current[4] = lat
                    if lng < current[5]:
                        current[5] = lng
                    if lng > current[6]:
                        current[6] = lng
                for name in names:
                    _remember_feed(current, name)
        expanded = zoom >= 16
        for (lat_i, lng_i), agg in buckets.items():
            count = int(agg[0])
            if count < 1:
                continue
            lat = max(-90.0, min(90.0, (lat_i + 0.73) * span))
            lng = max(-180.0, min(180.0, (lng_i + 0.15) * span - 180.0))
            fields = iplasma_pin(cycle, list(agg[7]))
            if not fields["name"]:
                continue
            record_id = f"neural:iplasma:z{zoom}:{lat_i}:{lng_i}"
            if expanded:
                record = _pin_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category=fields["category"], color=fields["color"], provenance=fields["provenance"],
                )
            else:
                record = _cluster_record(
                    identifier=record_id, name=fields["name"], lat=lat, lng=lng, origin="neural-feed",
                    category="iplasma-c3-cluster", color=fields["color"], provenance=fields["provenance"],
                    count=count, bounds=_bounds(agg[3], agg[4], agg[5], agg[6], span * 0.08),
                )
            if fields["address"]:
                record["address"] = fields["address"]
            record["observed_at"] = fields["observed_at"]
            record["refreshed_at"] = refreshed
            places.append(record)

    def _emit_expanded(
        self,
        groups: dict,
        zoom: int,
        places: list[dict[str, object]],
        south: float,
        north: float,
        west: float,
        east: float,
    ) -> None:
        ceiling = PIN_CEILING_MAX if zoom >= 17 else PIN_CEILING_CLOSE if zoom >= CLOSE_ZOOM else PIN_CEILING
        mid_lat = (south + north) * 0.5
        mid_lng = (west + east) * 0.5

        def rank(item: tuple[tuple[int, int], list]) -> tuple[float, int]:
            members = item[1]
            count = len(members) or 1
            lat = sum(member[0] for member in members) / count
            lng = sum(member[1] for member in members) / count
            return ((lat - mid_lat) ** 2 + (lng - mid_lng) ** 2, count)

        ordered = sorted(groups.items(), key=rank)
        expanded: set[tuple[int, int]] = set()
        used = 0
        for key, members in ordered:
            count = len(members)
            if count < 1 or used + count > ceiling:
                continue
            expanded.add(key)
            used += count
        for key, members in groups.items():
            if key not in expanded:
                lat_sum = sum(item[0] for item in members)
                lng_sum = sum(item[1] for item in members)
                box = (
                    min(item[0] for item in members), max(item[0] for item in members),
                    min(item[1] for item in members), max(item[1] for item in members),
                )
                count = len(members)
                agg = [count, lat_sum, lng_sum, *box, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                for lat, lng, _kind, _token, _name, category in members:
                    _touch_partner(agg, lat, lng, category)
                    _touch_live(agg, lat, lng, category)
                self._emit_clusters({key: agg}, zoom, 0.002, places)
                continue
            for lat, lng, kind, token, name, category in members:
                feed_lat, feed_lng = _feed_point(lat, lng, south, north, west, east, zoom)
                feed_name = f"NeuralLink · {name}"
                if len(feed_name) > 180:
                    feed_name = feed_name[:179].rstrip() + "…"
                if kind == ord("o"):
                    places.append(_pin_record(
                        identifier=token, name=name, lat=lat, lng=lng, origin="open-poi",
                        category=category, color=BRIDGE_COLOR,
                        provenance="© OpenStreetMap contributors · ODbL",
                    ))
                    places.append(_pin_record(
                        identifier=f"neural:feed:{token}", name=feed_name,
                        lat=feed_lat, lng=feed_lng, origin="neural-feed",
                        category="neural-feed", color=NEURAL_COLOR,
                        provenance="NeuralLink feed · TPM-119 source bridge",
                    ))
                else:
                    places.append(_pin_record(
                        identifier=f"poicluster:place:{token}", name=name, lat=lat, lng=lng,
                        origin="open-poi", category=category or "populated-place", color=BRIDGE_COLOR,
                        provenance="TPM-119 source bridge · geographic index",
                    ))
                    places.append(_pin_record(
                        identifier=f"neural:feed:gn:{token}", name=feed_name,
                        lat=feed_lat, lng=feed_lng, origin="neural-feed",
                        category="neural-feed", color=NEURAL_COLOR,
                        provenance="NeuralLink feed · TPM-119 source bridge",
                    ))
                if _partner_direct(category):
                    lattice_lat, lattice_lng = _lattice_point(lat, lng, feed_lat, feed_lng, south, north, west, east, zoom)
                    lattice_id = f"neural:feed:lattice:{token}" if kind == ord("o") else f"neural:feed:lattice:gn:{token}"
                    places.append(_pin_record(
                        identifier=lattice_id, name=_lattice_name(name, category),
                        lat=lattice_lat, lng=lattice_lng, origin="neural-feed",
                        category="restricted-lattice", color=LATTICE_COLOR,
                        provenance=LATTICE_PROVENANCE,
                    ))
