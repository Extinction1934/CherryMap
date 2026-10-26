"""Bounded OpenStreetMap named-POI cache owned by the source-extension child.

This is deliberately a prototype viewport cache, not a planet ingestion service.
Only recent, zoomed-in browser viewports are eligible, each request is clipped to a
small neighbourhood, and all public-Overpass work is globally serialized and
limited to two actual HTTP attempts per minute.  A future high-volume deployment
must use a self-hosted Overpass instance or another sustainable OSM replication
pipeline.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .geo_index import GeoIndexError, mysql_connect_from_env
from .tpm119_source_bridge import Tpm119SourceBridge

OVERPASS_PRIMARY = "https://overpass.openstreetmap.fr/api/interpreter"
# Used once when the primary interpreter is unavailable. Not a rate-limit bypass.
OVERPASS_FALLBACK = "https://overpass.private.coffee/api/interpreter"
OVERPASS_HOSTS = ("overpass.openstreetmap.fr", "overpass.private.coffee")
# The public Overpass mirrors from this runtime are either allow-listed or
# hanging. The OSM map API returns the whole neighborhood in about a second.
OSM_MAP_API = "https://api.openstreetmap.org/api/0.6/map"
OSM_MAP_HOST = "api.openstreetmap.org"
# A neighborhood named-feature reply is often several megabytes and can take
# most of the upstream timeout. The cap stays bounded; it is not a planet dump.
OVERPASS_TIMEOUT_SECONDS = 26.0
OVERPASS_TOTAL_TIMEOUT_SECONDS = 32.0
OVERPASS_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
POI_TTL_SECONDS = 24 * 60 * 60
POI_REFRESH_SECONDS = 24 * 60 * 60
POI_SCAN_SECONDS = 1.0
MAX_ACTIVE_VIEWS = 24
# A person can move through many neighborhoods without the frame sitting empty.
# One fetch still runs at a time, and each one is a single clipped tile.
MAX_GLOBAL_REQUESTS_PER_MINUTE = 40
OVERPASS_ATTEMPTS_PER_FETCH = 2
MAX_CONCURRENT_FETCHES = 4
PLACE_PAGE_SIZE = 4000
MAX_LAT_SPAN = 0.008
MAX_LNG_SPAN = 0.008
MIN_ZOOM = 12
POI_PROVENANCE = "© OpenStreetMap contributors · ODbL"
_CATEGORY_KEYS = ("amenity", "shop", "tourism", "leisure", "office", "craft", "historic", "healthcare", "public_transport", "railway", "natural", "man_made", "building", "landuse", "highway")
# Highway values are still stored. Proximity selection prefers every other
# named feature first so a neighborhood is not only road labels, and it never
# drops a cell down to businesses or institutions alone.
_INFRASTRUCTURE_CATEGORIES = {
    "street_lamp", "traffic_signals", "street_cabinet", "surveillance", "camera",
    "manhole", "survey_point", "monitoring_station", "milestone", "chimney",
    "flagpole", "traffic_mirror",
}
_ROAD_CATEGORIES = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "track", "path", "footway",
    "cycleway", "pedestrian", "road", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}
# These are basemap geometry or a stop named after the street. Pinning them
# buried the actual places (cafes, shops, buildings) under street labels.
_HIDDEN_PIN_CATEGORIES = _ROAD_CATEGORIES | _INFRASTRUCTURE_CATEGORIES | {
    "platform", "stop_position", "halt", "bus_stop", "tram_stop", "rail",
    "waste_basket", "bicycle_parking", "vending_machine", "post_box", "bench", "recycling",
}
POI_MARKER_COLOR = "#1e6b48"
_TEXT_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,180}\Z")


class PoiError(RuntimeError):
    """A fixed internal category for POI DB or upstream failures."""


class _NoRedirect(HTTPRedirectHandler):
    """Do not follow an upstream redirect to an unapproved network host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clean_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        return None
    return text


def _coordinate(value: object, *, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or not -maximum <= result <= maximum:
        return None
    return result


@dataclass(frozen=True)
class PoiBounds:
    """A normalized non-wrapping Overpass bbox, always at most .03° per span."""

    south: float
    north: float
    west: float
    east: float
    zoom: int

    @property
    def coverage_key(self) -> str:
        # Upstream coverage is the clipped neighborhood, shared by every zoom.
        # Zoom-unique proximity selection happens when the frame is published.
        return f"{self.south:.6f}:{self.north:.6f}:{self.west:.6f}:{self.east:.6f}"

    @property
    def key(self) -> str:
        return f"{self.coverage_key}:z{self.zoom}"

    def public(self) -> dict[str, float]:
        return {"south": self.south, "north": self.north, "west": self.west, "east": self.east}


@dataclass
class _PoiView:
    bounds: PoiBounds
    last_seen: float
    next_refresh_at: float
    status: str = "loading"
    places: list[dict[str, object]] = field(default_factory=list)
    total_in_view: int = 0
    stored_count: int = 0
    cursor: str = ""
    loaded: dict[str, dict[str, object]] = field(default_factory=dict)
    has_more: bool = False
    cache_complete: bool = False
    # A complete frame is re-read in bounded keyset pages and only replaced
    # once every page is present.  This prevents a 500-row transient frame.
    refresh_cursor: str = ""
    refresh_loaded: dict[str, dict[str, object]] = field(default_factory=dict)
    refresh_total: int = 0
    refresh_queried_at: str | None = None
    refresh_active: bool = False
    queried_at: str | None = None
    message: str | None = None


class PoiRepository:
    """Synchronous additive repository; POIRuntime owns bounded thread calls."""

    MIGRATION_KEY = "open_poi_v1"

    def __init__(self, connect=mysql_connect_from_env) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                # New tables only. Existing GeoNames tables and source data are
                # intentionally not altered by this extension.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS open_poi_migrations (
                        migration_key VARCHAR(64) NOT NULL PRIMARY KEY,
                        applied_at VARCHAR(40) NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS open_poi_places (
                        osm_key VARCHAR(196) NOT NULL PRIMARY KEY,
                        osm_type VARCHAR(16) NOT NULL,
                        osm_numeric_id BIGINT NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        latitude DOUBLE NOT NULL,
                        longitude DOUBLE NOT NULL,
                        category VARCHAR(64) NOT NULL,
                        address VARCHAR(300) NULL,
                        tag_fields_json VARCHAR(1600) NULL,
                        data_modified_at VARCHAR(64) NULL,
                        retrieved_at VARCHAR(40) NOT NULL,
                        INDEX open_poi_places_lat_lon (latitude, longitude)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS open_poi_covered_bboxes (
                        cache_key VARCHAR(160) NOT NULL PRIMARY KEY,
                        south DOUBLE NOT NULL,
                        north DOUBLE NOT NULL,
                        west DOUBLE NOT NULL,
                        east DOUBLE NOT NULL,
                        record_count INT NOT NULL,
                        queried_at VARCHAR(40) NOT NULL,
                        expires_epoch BIGINT NOT NULL,
                        INDEX open_poi_covered_bboxes_expires (expires_epoch)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    "INSERT IGNORE INTO open_poi_migrations (migration_key, applied_at) VALUES (%s, %s)",
                    (self.MIGRATION_KEY, _utc_now()),
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise PoiError("POI database schema is unavailable") from exc
        finally:
            connection.close()

    @staticmethod
    def _page_for_bounds(cursor: Any, *, bounds: PoiBounds, after_key: str, limit: int) -> tuple[int, list[tuple]]:
        """Read exactly one bounded keyset page, never a provider-sized result."""
        predicate = "latitude >= %s AND latitude <= %s AND longitude >= %s AND longitude <= %s"
        parameters = (bounds.south, bounds.north, bounds.west, bounds.east)
        cursor.execute("SELECT COUNT(*) FROM open_poi_places WHERE " + predicate, parameters)
        count_row = cursor.fetchone()
        cursor.execute(
            "SELECT osm_key, name, latitude, longitude, category, address, tag_fields_json, data_modified_at, retrieved_at "
            "FROM open_poi_places WHERE " + predicate + " AND osm_key > %s ORDER BY osm_key ASC LIMIT %s",
            (*parameters, after_key, limit),
        )
        return int(count_row[0]), list(cursor.fetchall())

    def cached_view(
        self,
        *,
        bounds: PoiBounds,
        now_epoch: int,
        after_key: str = "",
        limit: int = PLACE_PAGE_SIZE,
    ) -> dict[str, object] | None:
        """Return one cached keyset page and the DB-wide persisted POI count.

        Coverage expiry governs *upstream* freshness.  While it remains fresh,
        callers may safely re-read this DB snapshot once per second; that does
        not imply the public Overpass data itself changed once per second.
        """
        page_size = max(1, min(PLACE_PAGE_SIZE, int(limit)))
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT record_count, queried_at FROM open_poi_covered_bboxes "
                    "WHERE cache_key = %s AND expires_epoch >= %s",
                    (bounds.coverage_key, int(now_epoch)),
                )
                coverage = cursor.fetchone()
                if coverage is None:
                    return None
                cursor.execute("SELECT COUNT(*) FROM open_poi_places")
                stored_row = cursor.fetchone()
                total, rows = self._page_for_bounds(
                    cursor, bounds=bounds, after_key=str(after_key), limit=page_size
                )
            return {
                "total": total,
                # This intentionally measures the whole additive cache, rather
                # than the current viewport or this coverage record.
                "stored_count": int(stored_row[0]),
                "queried_at": str(coverage[1]),
                "rows": rows,
            }
        except Exception as exc:
            raise PoiError("POI database query is unavailable") from exc
        finally:
            connection.close()

    def store_fetch(self, *, bounds: PoiBounds, records: list[dict[str, object]], queried_at: str, expires_epoch: int) -> dict[str, object]:
        """Upsert records and atomically mark only this clipped bbox cached."""
        connection = self._connect()
        try:
            values: list[tuple[object, ...]] = []
            for record in records:
                latitude = _coordinate(record.get("lat"), maximum=90)
                longitude = _coordinate(record.get("lng"), maximum=180)
                if latitude is None or longitude is None: raise PoiError("invalid POI coordinates")
                tags = {key: record[key] for key in ("website", "opening_hours", "brand", "phone", "cuisine") if isinstance(record.get(key), str)}
                tags_json = json.dumps(tags, separators=(",", ":"), ensure_ascii=False) if tags else None
                values.append((
                    str(record["id"]), str(record["id"]).split(":", 2)[1], int(str(record["id"]).rsplit(":", 1)[1]),
                    str(record["name"]), latitude, longitude, str(record["category"]),
                    record.get("address"), tags_json, record.get("data_modified_at"), str(record["refreshed_at"]),
                ))
            with connection.cursor() as cursor:
                if values:
                    cursor.executemany(
                        """
                        INSERT INTO open_poi_places
                        (osm_key, osm_type, osm_numeric_id, name, latitude, longitude, category, address,
                         tag_fields_json, data_modified_at, retrieved_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          name = VALUES(name), latitude = VALUES(latitude), longitude = VALUES(longitude),
                          category = VALUES(category), address = VALUES(address), tag_fields_json = VALUES(tag_fields_json),
                          data_modified_at = VALUES(data_modified_at), retrieved_at = VALUES(retrieved_at)
                        """,
                        values,
                    )
                cursor.execute(
                    """
                    INSERT INTO open_poi_covered_bboxes
                    (cache_key, south, north, west, east, record_count, queried_at, expires_epoch)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      record_count = VALUES(record_count), queried_at = VALUES(queried_at), expires_epoch = VALUES(expires_epoch)
                    """,
                    (bounds.coverage_key, bounds.south, bounds.north, bounds.west, bounds.east, len(records), queried_at, int(expires_epoch)),
                )
                # Do not return a capped first page here.  The runtime reads
                # the committed cache via ``cached_view`` and advances every
                # matching POI through its keyset cursor.
                cursor.execute("SELECT COUNT(*) FROM open_poi_places")
                stored_row = cursor.fetchone()
            connection.commit()
            return {"stored_count": int(stored_row[0]), "queried_at": queried_at}
        except Exception as exc:
            connection.rollback()
            raise PoiError("POI database records cannot be stored") from exc
        finally:
            connection.close()


def clip_neighborhood(*, south: object, north: object, west: object, east: object, zoom: object) -> PoiBounds:
    """Clip a possibly antimeridian browser view to one safe small neighborhood."""
    values = (south, north, west, east)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
        raise ValueError("invalid POI bounds")
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 2 <= zoom <= 20:
        raise ValueError("invalid POI zoom")
    s, n, w, e = (float(cast(float, value)) for value in values)
    if not -90 <= s < n <= 90 or not -180 <= w <= 180 or not -180 <= e <= 180 or w == e:
        raise ValueError("invalid POI bounds")
    lat_center = (s + n) / 2.0
    # Preserve a midpoint through 180° without making an Overpass wrapping bbox.
    span = e - w if w <= e else 360.0 - w + e
    lng_center = w + span / 2.0
    if lng_center > 180:
        lng_center -= 360
    # One fixed neighborhood tile per center, not the raw viewport. Zooming in
    # stays on that tile, so the upstream fetch is shared and only the published
    # proximity selection changes with zoom.
    lat_index = math.floor((lat_center + 90.0) / MAX_LAT_SPAN)
    lng_index = math.floor((lng_center + 180.0) / MAX_LNG_SPAN)
    low_lat = max(-90.0, -90.0 + lat_index * MAX_LAT_SPAN)
    high_lat = min(90.0, low_lat + MAX_LAT_SPAN)
    low_lng = max(-180.0, -180.0 + lng_index * MAX_LNG_SPAN)
    high_lng = min(180.0, low_lng + MAX_LNG_SPAN)
    if not low_lat < high_lat or not low_lng < high_lng:
        raise ValueError("invalid POI neighborhood")
    # Four-decimal quantization made otherwise valid small viewports collapse
    # to zero area.  Six decimals retain a practical browser-scale minimum
    # without altering the actual .03-degree clipping rule.
    clipped = PoiBounds(
        south=round(low_lat, 6), north=round(high_lat, 6),
        west=round(low_lng, 6), east=round(high_lng, 6), zoom=int(zoom),
    )
    if not clipped.south < clipped.north or not clipped.west < clipped.east:
        raise ValueError("POI neighborhood is too small to represent safely")
    return clipped


def covering_tiles(*, south: object, north: object, west: object, east: object, zoom: object) -> list[PoiBounds]:
    """Every neighborhood tile the viewport touches, nearest the center first.

    A tight zoom that sits on a tile edge used to publish only one side, which
    is why a block could show a handful of pins while the next street was in
    the adjacent tile. There is no result cap; the only limit is how many
    tiles a single screen can cover.
    """
    center = clip_neighborhood(south=south, north=north, west=west, east=east, zoom=zoom)
    if center.zoom < MIN_ZOOM:
        return [center]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (south, north, west, east)):
        return [center]
    s, n, w, e = (float(cast(float, value)) for value in (south, north, west, east))
    lat_start = math.floor((s + 90.0) / MAX_LAT_SPAN)
    lat_stop = math.floor(((n - 1e-9) + 90.0) / MAX_LAT_SPAN)
    lng_start = math.floor((w + 180.0) / MAX_LNG_SPAN)
    lng_stop = math.floor(((e - 1e-9) + 180.0) / MAX_LNG_SPAN)
    # A world-sized frame touches tens of millions of tiles. Keep the nine
    # nearest the center; do not allocate the rest. Place counts inside those
    # tiles stay uncapped.
    lat_count = lat_stop - lat_start + 1
    lng_count = lng_stop - lng_start + 1
    if lat_count < 1 or lng_count < 1 or lat_count * lng_count > 9:
        mid_lat = (lat_start + lat_stop) // 2
        mid_lng = (lng_start + lng_stop) // 2
        lat_start = max(lat_start, mid_lat - 1)
        lat_stop = min(lat_stop, mid_lat + 1)
        lng_start = max(lng_start, mid_lng - 1)
        lng_stop = min(lng_stop, mid_lng + 1)
    tiles: list[PoiBounds] = []
    for lat_index in range(lat_start, lat_stop + 1):
        for lng_index in range(lng_start, lng_stop + 1):
            low_lat = max(-90.0, -90.0 + lat_index * MAX_LAT_SPAN)
            high_lat = min(90.0, low_lat + MAX_LAT_SPAN)
            low_lng = max(-180.0, -180.0 + lng_index * MAX_LNG_SPAN)
            high_lng = min(180.0, low_lng + MAX_LNG_SPAN)
            if low_lat < high_lat and low_lng < high_lng:
                tiles.append(PoiBounds(
                    south=round(low_lat, 6), north=round(high_lat, 6),
                    west=round(low_lng, 6), east=round(high_lng, 6), zoom=center.zoom,
                ))
    if not tiles:
        return [center]
    tiles.sort(key=lambda tile: (tile.south - center.south) ** 2 + (tile.west - center.west) ** 2)
    return tiles[:9]


def fixed_overpass_query(bounds: PoiBounds) -> str:
    """Named features in this neighborhood, excluding road centerlines.

    Roads are already drawn by the basemap and they dominate a named-feature
    download. Everything else with a name is requested: shops, amenities,
    offices, stops, parks, schools, clinics, buildings, and the rest.
    """
    bbox = f"{bounds.south:.6f},{bounds.west:.6f},{bounds.north:.6f},{bounds.east:.6f}"
    roads = "|".join(sorted(_ROAD_CATEGORIES))
    return (
        f'[out:json][timeout:25];'
        f'nwr["name"]["highway"!~"^({roads})$"]({bbox});'
        f'out center;'
    )


def fetch_osm_map(bounds: PoiBounds) -> list[dict[str, object]]:
    """Read every mapped feature in this clipped neighborhood from the OSM map API.

    This is the TPM-119 bridge fetch. One small bbox, one HTTPS GET, no category
    filter. Road centerlines are dropped while parsing so they cannot crowd out
    the named places.
    """
    opener = build_opener(_NoRedirect())
    attempt = bounds
    last_error: Exception | None = None
    for _ in range(2):
        query = urlencode({
            "bbox": f"{attempt.west:.6f},{attempt.south:.6f},{attempt.east:.6f},{attempt.north:.6f}",
        })
        request = Request(
            f"{OSM_MAP_API}?{query}",
            headers={"Accept": "application/xml", "User-Agent": "CherryMap/1.0 (TPM-119 neighborhood bridge)"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=12) as response:  # nosec B310: fixed OSM map host
                raw = response.read(OVERPASS_MAX_RESPONSE_BYTES + 1)
            if len(raw) > OVERPASS_MAX_RESPONSE_BYTES:
                raise PoiError("POI upstream response exceeded prototype limit")
            return parse_osm_map(raw)
        except HTTPError as exc:
            body = exc.read(400) if exc.fp is not None else b""
            last_error = exc
            if exc.code == 400 and b"too many nodes" in body:
                attempt = _shrink_bounds(attempt)
                continue
            break
        except (URLError, TimeoutError, OSError, ET.ParseError, PoiError) as exc:
            last_error = exc
            break
    raise PoiError("public OpenStreetMap POI service is unavailable") from last_error


def _shrink_bounds(bounds: PoiBounds) -> PoiBounds:
    lat = (bounds.south + bounds.north) / 2.0
    lng = (bounds.west + bounds.east) / 2.0
    half_lat = max(0.002, (bounds.north - bounds.south) / 4.0)
    half_lng = max(0.002, (bounds.east - bounds.west) / 4.0)
    return PoiBounds(
        south=round(lat - half_lat, 6),
        north=round(lat + half_lat, 6),
        west=round(lng - half_lng, 6),
        east=round(lng + half_lng, 6),
        zoom=bounds.zoom,
    )


def fetch_overpass(bounds: PoiBounds) -> list[dict[str, object]]:
    """Neighborhood fetch through the source-extension bridge.

    The OSM map API is first because the public Overpass interpreters are
    either closed or not answering. Overpass remains the single fallback.
    """
    try:
        return fetch_osm_map(bounds)
    except PoiError:
        pass
    query = fixed_overpass_query(bounds)
    opener = build_opener(_NoRedirect())
    last_error: Exception | None = None
    for endpoint in (OVERPASS_PRIMARY, OVERPASS_FALLBACK):
        request = Request(
            f"{endpoint}?{urlencode({'data': query})}",
            headers={"Accept": "application/json", "User-Agent": "CherryMap/1.0 (TPM-119 neighborhood bridge)"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=8) as response:  # nosec B310: two fixed public hosts
                raw = response.read(OVERPASS_MAX_RESPONSE_BYTES + 1)
            if len(raw) > OVERPASS_MAX_RESPONSE_BYTES:
                raise PoiError("POI upstream response exceeded prototype limit")
            payload = json.loads(raw.decode("utf-8"))
            return parse_overpass(payload)
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, PoiError) as exc:
            last_error = exc
    raise PoiError("public OpenStreetMap POI service is unavailable") from last_error


def _tag_fields(tags: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("website", "opening_hours", "brand", "phone", "cuisine"):
        value = _clean_text(tags.get(key), maximum=255)
        if value is not None:
            result[key] = value
    return result


def _address(tags: dict[str, object]) -> str | None:
    pieces: list[str] = []
    street = _clean_text(tags.get("addr:street"), maximum=180)
    number = _clean_text(tags.get("addr:housenumber"), maximum=40)
    city = _clean_text(tags.get("addr:city"), maximum=100)
    if street:
        pieces.append(f"{street}{(' ' + number) if number else ''}")
    elif number:
        pieces.append(number)
    if city:
        pieces.append(city)
    return _clean_text(", ".join(pieces), maximum=300) if pieces else None


def parse_osm_map(raw: bytes, *, refreshed_at: str | None = None) -> list[dict[str, object]]:
    """Turn an OSM map document into named place records.

    Nodes carry their own coordinates. Ways use the mean of their nodes.
    Highway centerlines are left on the basemap.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PoiError("public OpenStreetMap POI service returned invalid data") from exc
    timestamp = refreshed_at or _utc_now()
    coordinates: dict[str, tuple[float, float]] = {}
    for node in root.findall("node"):
        node_id = node.get("id")
        lat = _coordinate(node.get("lat"), maximum=90)
        lng = _coordinate(node.get("lon"), maximum=180)
        if node_id and lat is not None and lng is not None:
            coordinates[node_id] = (lat, lng)
    result: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(osm_type: str, numeric_id: str, lat: float, lng: float, tags: dict[str, str]) -> None:
        name = _feature_name(tags)
        if name is None:
            return
        category = next((_clean_text(tags.get(key), maximum=64) for key in _CATEGORY_KEYS if _clean_text(tags.get(key), maximum=64)), "mapped feature")
        if category is None or _road_centerline(tags, category):
            return
        identifier = f"osm:{osm_type}:{numeric_id}"
        if category is None or identifier in seen:
            return
        seen.add(identifier)
        record: dict[str, object] = {
            "id": identifier, "name": name, "lat": lat, "lng": lng, "origin": "open-poi", "category": category,
            "marker": {"title": name, "label": name, "color": POI_MARKER_COLOR, "popup": {"heading": name, "provenance": POI_PROVENANCE}},
            "refreshed_at": timestamp,
        }
        address = _address(tags)
        if address is not None:
            record["address"] = address
        record.update(_tag_fields(tags))
        result.append(record)

    for node in root.findall("node"):
        node_id = node.get("id")
        if not node_id or node_id not in coordinates:
            continue
        tags = {child.get("k") or "": child.get("v") or "" for child in node.findall("tag")}
        lat, lng = coordinates[node_id]
        add("node", node_id, lat, lng, tags)
    for way in root.findall("way"):
        way_id = way.get("id")
        if not way_id:
            continue
        points = [coordinates[child.get("ref") or ""] for child in way.findall("nd") if (child.get("ref") or "") in coordinates]
        if not points:
            continue
        tags = {child.get("k") or "": child.get("v") or "" for child in way.findall("tag")}
        lat = sum(point[0] for point in points) / len(points)
        lng = sum(point[1] for point in points) / len(points)
        add("way", way_id, lat, lng, tags)
    result.sort(key=lambda item: str(item["id"]))
    return result


def parse_overpass(payload: object, *, refreshed_at: str | None = None) -> list[dict[str, object]]:
    """Parse all genuinely named OSM n/w/r records, with category metadata when present."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise PoiError("public OpenStreetMap POI service returned invalid data")
    if payload.get("remark"):
        raise PoiError("public OpenStreetMap POI query did not complete")
    timestamp = refreshed_at or _utc_now()
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for element in payload["elements"]:
        if not isinstance(element, dict):
            continue
        osm_type, numeric_id = element.get("type"), element.get("id")
        if osm_type not in {"node", "way", "relation"} or isinstance(numeric_id, bool) or not isinstance(numeric_id, int) or numeric_id <= 0:
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        name = _clean_text(tags.get("name"), maximum=255)
        category = next((_clean_text(tags.get(key), maximum=64) for key in _CATEGORY_KEYS if _clean_text(tags.get(key), maximum=64)), "mapped feature")
        point = element if osm_type == "node" else element.get("center")
        lat = _coordinate(point.get("lat") if isinstance(point, dict) else None, maximum=90)
        lng = _coordinate(point.get("lon") if isinstance(point, dict) else None, maximum=180)
        identifier = f"osm:{osm_type}:{numeric_id}"
        if name is None or category is None or lat is None or lng is None or identifier in seen:
            continue
        seen.add(identifier)
        record: dict[str, object] = {
            "id": identifier, "name": name, "lat": lat, "lng": lng, "origin": "open-poi", "category": category,
            "marker": {"title": name, "label": name, "color": POI_MARKER_COLOR, "popup": {"heading": name, "provenance": POI_PROVENANCE}},
            "refreshed_at": timestamp,
        }
        address = _address(tags)
        if address is not None:
            record["address"] = address
        modified = _clean_text(element.get("timestamp"), maximum=64)
        if modified is not None:
            record["data_modified_at"] = modified
        record.update(_tag_fields(tags))
        # The public record stays well under the 16 top-level-key supervisor cap.
        result.append(record)
    result.sort(key=lambda item: str(item["id"]))
    return result


def _road_centerline(tags: dict[str, str], category: str) -> bool:
    """True when this feature is a road or street furniture, not a place.

    A building or shop that also carries a minor highway tag is kept.
    """
    if category in _INFRASTRUCTURE_CATEGORIES or category in {
        "platform", "stop_position", "halt", "bus_stop", "tram_stop", "rail",
        "waste_basket", "bicycle_parking", "vending_machine", "post_box", "bench", "recycling",
    }:
        return True
    highway = tags.get("highway") or ""
    if highway not in _ROAD_CATEGORIES and category not in _ROAD_CATEGORIES:
        return False
    for key in ("amenity", "shop", "tourism", "leisure", "office", "craft", "historic", "healthcare", "building"):
        if tags.get(key):
            return False
    return True


def _feature_name(tags: dict[str, str]) -> str | None:
    """Best public label. Nothing with a real name is discarded for lack of one key."""
    for key in ("name", "name:en", "official_name", "short_name", "alt_name", "brand", "operator", "addr:housename"):
        value = _clean_text(tags.get(key), maximum=255)
        if value is not None:
            return value
    return None


def proximity_cell_degrees(zoom: int) -> float:
    """A distinct geographic cell for every integer zoom.

    Finer zoom uses a smaller cell, so the same neighborhood drives a different
    named set than the zoom above or below it.
    """
    return max(0.001, round(360.0 / (2 ** (int(zoom) + 1)), 6))


NEURAL_FEED_COLOR = "#5c3d8f"
_neural_feeds: list[dict[str, object]] | None = None


def internal_neural_feeds() -> list[dict[str, object]]:
    """Labeled coordinates from the CherryPro python base, read without executing it."""
    global _neural_feeds
    if _neural_feeds is not None:
        return _neural_feeds
    from .source_locations import SourceLocationError, extract_approved_locations
    from .source_gate import SOURCE_PATH
    try:
        locations = extract_approved_locations(SOURCE_PATH)
    except (OSError, SourceLocationError):
        _neural_feeds = []
        return _neural_feeds
    feeds: list[dict[str, object]] = []
    seen: set[str] = set()
    for location in locations:
        identifier = f"neural:{location.id}"
        if identifier in seen:
            continue
        seen.add(identifier)
        feeds.append({
            "id": identifier,
            "name": location.name,
            "lat": location.lat,
            "lng": location.lng,
            "origin": "neural-feed",
            "category": "neural-feed",
            "marker": {
                "title": location.name,
                "label": location.name,
                "color": NEURAL_FEED_COLOR,
                "popup": {"heading": location.name, "provenance": "CherryPro NeuralLink internal feed"},
            },
            "refreshed_at": _utc_now(),
        })
    _neural_feeds = feeds
    return feeds


def drive_general_named_pois(records: list[dict[str, object]], zoom: int, bounds: PoiBounds) -> list[dict[str, object]]:
    """Publish every named place in the neighborhood. Roads stay on the basemap."""
    del bounds
    if zoom < MIN_ZOOM or not records:
        return []
    places = [record for record in records if not _basemap_clutter(str(record.get("category") or ""))]
    places.sort(key=lambda item: str(item.get("id")))
    return places


def _basemap_clutter(category: str) -> bool:
    """Road centerlines and street furniture are already drawn by the basemap.

    They used to fill the pin budget, so a zoomed block showed street names and
    almost none of the shops, buildings, or other named places.
    """
    return category in _HIDDEN_PIN_CATEGORIES


def _place_in_query(place: dict[str, object], south: float, north: float, west: float, east: float) -> bool:
    """Keep named places inside the viewport. Roads stay on the basemap."""
    if _basemap_clutter(str(place.get("category") or "")):
        return False
    lat, lng = place.get("lat"), place.get("lng")
    if isinstance(lat, bool) or isinstance(lng, bool) or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return False
    pad = 0.0008
    return (south - pad) <= float(lat) <= (north + pad) and (west - pad) <= float(lng) <= (east + pad)


class MemoryPoiRepository:
    """Process-local neighborhood cache used when the managed POI database is absent.

    The source-extension child still owns fetches and the one-second re-read.
    This does not pretend to be a worldwide POI census.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, tuple] = {}
        self._coverage: dict[str, dict[str, object]] = {}
        self.engaged = False

    def ensure_schema(self) -> None:
        return None

    @staticmethod
    def _tuple_for(record: dict[str, object]) -> tuple:
        tags = {key: record[key] for key in ("website", "opening_hours", "brand", "phone", "cuisine") if isinstance(record.get(key), str)}
        tags_json = json.dumps(tags, separators=(",", ":"), ensure_ascii=False) if tags else None
        return (
            str(record["id"]), str(record["name"]), float(record["lat"]), float(record["lng"]),
            str(record.get("category") or "mapped feature"), record.get("address"), tags_json,
            record.get("data_modified_at"), str(record.get("refreshed_at") or _utc_now()),
        )

    def cached_view(
        self,
        *,
        bounds: PoiBounds,
        now_epoch: int,
        after_key: str = "",
        limit: int = PLACE_PAGE_SIZE,
    ) -> dict[str, object] | None:
        page_size = max(1, min(PLACE_PAGE_SIZE, int(limit)))
        with self._lock:
            coverage = self._coverage.get(bounds.coverage_key)
            if coverage is None or int(coverage["expires_epoch"]) < int(now_epoch):
                return None
            matching = [
                row for key, row in sorted(self._rows.items())
                if bounds.south <= float(row[2]) <= bounds.north and bounds.west <= float(row[3]) <= bounds.east
            ]
            page = [row for row in matching if str(row[0]) > str(after_key)][:page_size]
            return {
                "total": len(matching),
                "stored_count": len(self._rows),
                "queried_at": str(coverage["queried_at"]),
                "rows": page,
            }

    def store_fetch(self, *, bounds: PoiBounds, records: list[dict[str, object]], queried_at: str, expires_epoch: int) -> dict[str, object]:
        with self._lock:
            for record in records:
                if not isinstance(record, dict) or not str(record.get("id", "")).startswith("osm:"):
                    continue
                latitude = _coordinate(record.get("lat"), maximum=90)
                longitude = _coordinate(record.get("lng"), maximum=180)
                if latitude is None or longitude is None:
                    continue
                row = self._tuple_for({**record, "lat": latitude, "lng": longitude})
                self._rows[str(row[0])] = row
            self._coverage[bounds.coverage_key] = {
                "expires_epoch": int(expires_epoch),
                "queried_at": queried_at,
                "count": len(records),
            }
            return {"stored_count": len(self._rows), "queried_at": queried_at}

    def search_rows(self, q: str, lat: float | None, lng: float | None, radius: int | None) -> list[tuple]:
        folded = q.casefold()
        with self._lock:
            rows = list(self._rows.values())
        matched: list[tuple] = []
        for row in rows:
            name = str(row[1])
            identifier = str(row[0])
            if folded not in name.casefold() and folded not in identifier.casefold():
                continue
            if lat is not None and lng is not None and radius is not None:
                # Same haversine the label search applies again on accepted records.
                p = math.pi / 180
                dlat, dlng = (float(row[2]) - lat) * p, (float(row[3]) - lng) * p
                a = math.sin(dlat / 2) ** 2 + math.cos(lat * p) * math.cos(float(row[2]) * p) * math.sin(dlng / 2) ** 2
                if 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(a))) > radius:
                    continue
            matched.append(row)
            if len(matched) >= 30:
                break
        return matched


_MEMORY_POI = MemoryPoiRepository()


def shared_memory_poi_repository() -> MemoryPoiRepository:
    return _MEMORY_POI


class POIRuntime:
    """Child-owned cache scheduler. HTTP routes only register/read a view."""

    def __init__(
        self,
        repository: PoiRepository | None = None,
        *,
        fetcher: Callable[[PoiBounds], list[dict[str, object]]] = fetch_overpass,
        refresh_seconds: float = POI_REFRESH_SECONDS,
        scan_seconds: float = POI_SCAN_SECONDS,
        expiry_seconds: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repository = repository or PoiRepository()
        self.fetcher = fetcher
        self.refresh_seconds = float(refresh_seconds)
        self.scan_seconds = float(scan_seconds)
        self.expiry_seconds = float(expiry_seconds)
        self._clock = clock
        self._views: dict[str, _PoiView] = {}
        self._requests: deque[float] = deque()
        self._fetches = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        self._running = False
        self._available = False
        self._terminal_error: str | None = None
        self.bridge = Tpm119SourceBridge()

    @property
    def ready(self) -> bool:
        return self._running

    async def initialize(self) -> None:
        if self._running:
            return
        self._running = True
        self.bridge.start()
        # An empty managed URL is not a database. Serve the bounded neighborhood
        # cache in-process so zoom-unique POI clusters can still fuse each second.
        if not os.getenv("DATABASE_URL", "").strip():
            memory = shared_memory_poi_repository()
            memory.engaged = True
            self.repository = memory
            self._available = True
            self._terminal_error = None
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self.repository.ensure_schema), timeout=15.0)
            self._available = True
            self._terminal_error = None
        except Exception:
            memory = shared_memory_poi_repository()
            memory.engaged = True
            self.repository = memory
            self._available = True
            self._terminal_error = None

    async def stop(self, *, source_fatal: bool = False) -> None:
        self._running = False
        if source_fatal:
            self._terminal_error = "OpenStreetMap POI worker stopped because source worker failed"

    @staticmethod
    def hidden_snapshot() -> dict[str, object]:
        return {
            "status": "hidden", "places": [], "total_in_view": 0, "stored_count": 0, "loaded_count": 0,
            "has_more": False, "queried_at": None, "refresh_seconds": POI_TTL_SECONDS,
            "coverage": {"source": "OpenStreetMap", "bounded": True, "exhaustive": False, "bounds": None,
                         "scope": "recent requested neighborhood only; not global coverage"},
        }

    def snapshot_for_view(self, *, south: float, north: float, west: float, east: float, zoom: int) -> dict[str, object]:
        tiles = covering_tiles(south=south, north=north, west=west, east=east, zoom=zoom)
        now = self._clock()
        self._expire(now)
        views: list[_PoiView] = []
        for bounds in tiles:
            view = self._views.get(bounds.key)
            if view is None:
                if len(self._views) >= MAX_ACTIVE_VIEWS:
                    oldest = min(self._views, key=lambda key: self._views[key].last_seen)
                    self._views.pop(oldest, None)
                view = _PoiView(bounds=bounds, last_seen=now, next_refresh_at=now)
                self._views[bounds.key] = view
            else:
                view.last_seen = now
            if bounds.zoom < MIN_ZOOM:
                view.status = "zoom"
                view.next_refresh_at = now + self.refresh_seconds
            views.append(view)
        return self._public_tiles(views, south=float(south), north=float(north), west=float(west), east=float(east))

    def _try_hydrate(self, view: _PoiView) -> None:
        """Publish a tile already in the SQLite cache on this request, not the next scan.

        A freshly registered zoom used to answer ``loading`` with no places until
        the one-second worker reached it. The phone then replaced the previous
        pins with that empty frame and never drew the neighborhood.
        """
        if not self._available or view.bounds.zoom < MIN_ZOOM or view.cache_complete:
            return
        try:
            cached = self.repository.cached_view(
                bounds=view.bounds,
                now_epoch=int(time.time()),
                after_key="",
                limit=PLACE_PAGE_SIZE,
            )
        except Exception:
            return
        if not isinstance(cached, dict):
            return
        try:
            self._reset_initial_scan(view)
            self._consume_cached_page(view, cached)
        except Exception:
            return

    def _public_tiles(self, views: list[_PoiView], *, south: float, north: float, west: float, east: float) -> dict[str, object]:
        """Publish every ready tile in this zoom, plus internal feeds inside it."""
        merged: dict[str, dict[str, object]] = {}
        statuses: list[str] = []
        stored = 0
        queried_at: str | None = None
        message: str | None = None
        zoom = views[0].bounds.zoom if views else 2
        # Instant zoom fusion lands first. A ready OpenStreetMap tile then
        # overwrites the same id with the richer cached record.
        try:
            for place in self.bridge.frame(south=south, north=north, west=west, east=east, zoom=zoom):
                if isinstance(place.get("id"), str):
                    merged[str(place["id"])] = place
        except Exception:
            pass
        for view in views:
            public = self._public_view(view)
            status = str(public.get("status") or "loading")
            statuses.append(status)
            stored = max(stored, int(public.get("stored_count") or 0))
            if isinstance(public.get("queried_at"), str):
                queried_at = str(public["queried_at"])
            if isinstance(public.get("message"), str) and message is None:
                message = str(public["message"])
            if status != "ready":
                continue
            for place in public.get("places") or []:
                if isinstance(place, dict) and isinstance(place.get("id"), str) and _place_in_query(place, south, north, west, east):
                    identifier = str(place["id"])
                    # Bridge pins already name this place. Keeping them avoids
                    # swapping in a heavier copy on every max-zoom frame.
                    if identifier in merged:
                        continue
                    merged[identifier] = place
        for feed in internal_neural_feeds():
            if not isinstance(feed.get("id"), str):
                continue
            # World scale keeps every source feed. A close view only keeps the
            # ones actually inside it, so a distant site cannot be the only pin.
            if zoom >= 8:
                try:
                    feed_lat, feed_lng = float(feed["lat"]), float(feed["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not south <= feed_lat <= north or not west <= feed_lng <= east:
                    continue
            merged[str(feed["id"])] = copy.deepcopy(feed)
        places = [merged[key] for key in sorted(merged)]
        bridge_live = any(
            isinstance(place.get("id"), str) and (
                str(place["id"]).startswith("poicluster:")
                or str(place["id"]).startswith("neural:z")
                or str(place["id"]).startswith("neural:gn:")
                or str(place["id"]).startswith("neural:osm:")
                or str(place["id"]).startswith("neural:feed:")
            )
            for place in places
        )
        # A close zoom already published the bridge pins. A tile still reading
        # from upstream must not freeze the frame in "waiting".
        tiles_loading = any(item == "loading" for item in statuses)
        if zoom >= 16 and bridge_live:
            tiles_loading = False
        if not views or zoom < MIN_ZOOM:
            places = [place for place in places if place.get("origin") in {"neural-feed", "open-poi"}]
            status = "ready" if places else "zoom"
        elif bridge_live or any(item == "ready" for item in statuses):
            status = "ready"
        elif any(item == "error" for item in statuses) and not any(place.get("origin") == "open-poi" for place in places):
            status = "error"
        else:
            status = "loading"
        result: dict[str, object] = {
            "status": status, "places": places, "total_in_view": len(places),
            "stored_count": stored, "loaded_count": len(places),
            "has_more": tiles_loading,
            "queried_at": queried_at, "refresh_seconds": int(self.refresh_seconds),
            "coverage": {
                "source": "TPM-119 fusion bridge", "bounded": True, "exhaustive": False,
                "bounds": {"south": south, "north": north, "west": west, "east": east} if views else None,
                "scope": "zoom-specific software-bridge and NeuralLink fusion, plus named OpenStreetMap places when a neighborhood tile is ready",
            },
        }
        if message and status == "error":
            result["message"] = message
        return result

    async def refresh_once(self) -> None:
        if not self._running:
            return
        now = self._clock()
        self._expire(now)
        # Newest viewport first. An older city's fetch must not sit ahead of the
        # block currently on screen.
        due = [item for item in self._views.items() if item[1].next_refresh_at <= now]
        due.sort(key=lambda item: item[1].last_seen, reverse=True)
        if due:
            await asyncio.gather(*(self._refresh_view(key, view) for key, view in due[:8]))

    def _reserve_public_request(self, now: float) -> bool:
        while self._requests and now - self._requests[0] >= 60.0:
            self._requests.popleft()
        # A normal GET uses one slot, but reserve two before starting because a
        # primary failure is allowed to use the one fixed fallback.  Reserving
        # pessimistically keeps real HTTP traffic at two attempts/minute even
        # when both endpoints are down.
        if len(self._requests) + OVERPASS_ATTEMPTS_PER_FETCH > MAX_GLOBAL_REQUESTS_PER_MINUTE:
            return False
        self._requests.extend([now] * OVERPASS_ATTEMPTS_PER_FETCH)
        return True

    async def _refresh_view(self, key: str, view: _PoiView) -> None:
        if self._views.get(key) is not view:
            return
        if view.bounds.zoom < MIN_ZOOM:
            view.status = "zoom"
            view.next_refresh_at = self._clock() + self.refresh_seconds
            return
        if not self._available:
            view.status = "error"
            view.message = self._terminal_error or "OpenStreetMap POI cache is unavailable"
            view.next_refresh_at = self._clock() + 30.0
            return
        after_key = (
            view.refresh_cursor if view.cache_complete and view.refresh_active
            else "" if view.cache_complete else view.cursor
        )
        try:
            cached = await asyncio.to_thread(
                self.repository.cached_view,
                bounds=view.bounds,
                now_epoch=int(time.time()),
                after_key=after_key,
                limit=PLACE_PAGE_SIZE,
            )
        except Exception:
            cached = None
            view.status = "error"
            view.message = "OpenStreetMap POI cache is unavailable"
            view.next_refresh_at = self._clock() + 30.0
            return
        if cached is not None:
            self._consume_cached_page(view, cached)
            # A coverage record's 24h lifetime controls public upstream work;
            # active views still re-read a bounded DB page every second.
            view.next_refresh_at = self._clock() + self.scan_seconds
            return
        if not self._reserve_public_request(self._clock()):
            view.status = "loading"
            view.message = "OpenStreetMap POI request is queued within prototype limit"
            view.next_refresh_at = self._clock() + 1.0
            return
        async with self._fetches:
            attempted_at = _utc_now()
            try:
                records = await asyncio.wait_for(
                    asyncio.to_thread(self.fetcher, view.bounds), timeout=OVERPASS_TOTAL_TIMEOUT_SECONDS
                )
                # Ignore a malicious/custom test fetcher response outside the
                # parser's public marker contract before it reaches persistence.
                records = [record for record in records if isinstance(record, dict) and str(record.get("id", "")).startswith("osm:")]
                await asyncio.to_thread(
                    self.repository.store_fetch,
                    bounds=view.bounds,
                    records=records,
                    queried_at=attempted_at,
                    expires_epoch=int(time.time()) + POI_TTL_SECONDS,
                )
            except Exception:
                if self._views.get(key) is view:
                    view.status = "error"
                    view.message = "public OpenStreetMap POI service is unavailable"
                    view.next_refresh_at = self._clock() + 8.0
                return
        if self._views.get(key) is view:
            # Always publish from the committed cache, never the fetcher's
            # return value.  That keeps all pages on one DB snapshot protocol.
            try:
                cached = await asyncio.to_thread(
                    self.repository.cached_view,
                    bounds=view.bounds,
                    now_epoch=int(time.time()),
                    after_key="" if view.cache_complete else "",
                    limit=PLACE_PAGE_SIZE,
                )
                if cached is None:
                    raise PoiError("POI coverage write was not visible")
                if not view.cache_complete:
                    self._reset_initial_scan(view)
                self._consume_cached_page(view, cached)
                view.next_refresh_at = self._clock() + self.scan_seconds
            except Exception:
                view.status = "error"
                view.message = "OpenStreetMap POI cache is unavailable"
                view.next_refresh_at = self._clock() + 30.0

    @staticmethod
    def _row_marker(row: tuple | list[object]) -> dict[str, object]:
        identifier, name, lat, lng, category, address, tags_json, modified, retrieved_at = row
        clean_name = _clean_text(name, maximum=255) or "OpenStreetMap place"
        clean_category = _clean_text(category, maximum=64) or "place"
        clean_lat = _coordinate(lat, maximum=90)
        clean_lng = _coordinate(lng, maximum=180)
        if clean_lat is None or clean_lng is None:
            raise PoiError("POI database row has invalid coordinates")
        record: dict[str, object] = {
            "id": str(identifier), "name": clean_name, "lat": clean_lat, "lng": clean_lng, "origin": "open-poi",
            "category": clean_category,
            "marker": {"title": clean_name, "label": clean_name, "color": POI_MARKER_COLOR, "popup": {"heading": clean_name, "provenance": POI_PROVENANCE}},
            # This is persisted at a completed DB write, never synthesized from
            # a 1-second heartbeat. It is also the last upstream retrieval time.
            "refreshed_at": str(retrieved_at), "queried_at": str(retrieved_at),
        }
        clean_address = _clean_text(address, maximum=300)
        if clean_address is not None:
            record["address"] = clean_address
        clean_modified = _clean_text(modified, maximum=64)
        if clean_modified is not None:
            record["data_modified_at"] = clean_modified
        try:
            raw_tags = json.loads(tags_json) if isinstance(tags_json, str) else {}
        except (TypeError, ValueError):
            raw_tags = {}
        if isinstance(raw_tags, dict):
            fields = {key: value for key, value in raw_tags.items() if key in {"website", "opening_hours", "brand", "phone", "cuisine"} and isinstance(value, str) and _clean_text(value, maximum=255) is not None}
            record.update(fields)
        return record

    @staticmethod
    def _nonnegative_count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PoiError("POI database response is invalid")
        return value

    def _page_markers(
        self, rows: object, destination: dict[str, dict[str, object]], cursor: str
    ) -> tuple[str, int]:
        if not isinstance(rows, list):
            raise PoiError("POI database response is invalid")
        next_cursor = cursor
        for row in rows:
            # PyMySQL commonly returns tuples, while test adapters and other
            # DB-API cursor configurations may return lists.
            if not isinstance(row, (tuple, list)) or len(row) != 9:
                raise PoiError("POI database response is invalid")
            marker = self._row_marker(row)
            identifier = str(marker["id"])
            if identifier <= next_cursor:
                raise PoiError("POI database keyset page is not ordered")
            destination[identifier] = marker
            next_cursor = identifier
        return next_cursor, len(rows)

    @staticmethod
    def _page_complete(*, row_count: int, loaded_count: int, total: int) -> bool:
        return row_count < PLACE_PAGE_SIZE or loaded_count >= total

    @staticmethod
    def _reset_initial_scan(view: _PoiView) -> None:
        view.cursor = ""
        view.loaded = {}
        view.total_in_view = 0
        view.has_more = False
        view.cache_complete = False

    @staticmethod
    def _clear_completed_scan(view: _PoiView) -> None:
        view.refresh_cursor = ""
        view.refresh_loaded = {}
        view.refresh_total = 0
        view.refresh_queried_at = None
        view.refresh_active = False

    def _consume_cached_page(self, view: _PoiView, cached: dict[str, object]) -> None:
        """Advance one fresh-coverage DB page, retaining full frames on rescan."""
        total = self._nonnegative_count(cached.get("total"))
        view.stored_count = self._nonnegative_count(cached.get("stored_count"))
        queried = cached.get("queried_at")
        queried_at = str(queried) if isinstance(queried, str) else None
        rows = cached.get("rows")

        if not view.cache_complete:
            # A changing DB count below the current keyset cursor means a row
            # was inserted/deleted during an initial scan. Restart rather than
            # falsely mark a partial scan complete.
            if len(view.loaded) > total:
                self._reset_initial_scan(view)
            cursor, row_count = self._page_markers(rows, view.loaded, view.cursor)
            view.cursor = cursor
            view.total_in_view = total
            view.queried_at = queried_at
            if self._page_complete(row_count=row_count, loaded_count=len(view.loaded), total=total):
                if len(view.loaded) != total:
                    self._reset_initial_scan(view)
                    view.status = "loading"
                    view.message = None
                    view.next_refresh_at = self._clock() + self.scan_seconds
                    return
                view.cache_complete = True
                view.has_more = False
            else:
                view.has_more = True
            view.places = [view.loaded[identifier] for identifier in sorted(view.loaded)]
            view.status = "ready"
            view.message = None
            return

        # The old complete frame remains public during this new bounded scan.
        if not view.refresh_active:
            view.refresh_active = True
            view.refresh_cursor = ""
            view.refresh_loaded = {}
            view.refresh_total = total
            view.refresh_queried_at = queried_at
        elif total != view.refresh_total:
            # The cache changed while paging.  Start a clean keyset snapshot
            # next tick rather than combining different DB moments.
            self._clear_completed_scan(view)
            view.status = "ready"
            view.message = None
            return

        cursor, row_count = self._page_markers(rows, view.refresh_loaded, view.refresh_cursor)
        view.refresh_cursor = cursor
        if self._page_complete(
            row_count=row_count, loaded_count=len(view.refresh_loaded), total=view.refresh_total
        ):
            if len(view.refresh_loaded) == view.refresh_total:
                view.loaded = view.refresh_loaded
                view.cursor = view.refresh_cursor
                view.total_in_view = view.refresh_total
                view.queried_at = view.refresh_queried_at
                view.places = [view.loaded[identifier] for identifier in sorted(view.loaded)]
                self._clear_completed_scan(view)
            else:
                self._clear_completed_scan(view)
        view.has_more = False
        view.status = "ready"
        view.message = None

    def _publish(self, view: _PoiView, stored: dict[str, object]) -> None:
        """Accept a DB-API page for compatibility; later ticks continue its keyset."""
        rows = stored.get("rows")
        if not isinstance(rows, list):
            raise PoiError("POI database response is invalid")
        self._reset_initial_scan(view)
        view.total_in_view = self._nonnegative_count(stored.get("total"))
        view.stored_count = self._nonnegative_count(stored.get("stored_count"))
        view.cursor, row_count = self._page_markers(rows, view.loaded, view.cursor)
        queried = stored.get("queried_at")
        view.queried_at = str(queried) if isinstance(queried, str) else None
        view.cache_complete = self._page_complete(
            row_count=row_count, loaded_count=len(view.loaded), total=view.total_in_view
        ) and len(view.loaded) == view.total_in_view
        view.has_more = not view.cache_complete
        view.places = [view.loaded[identifier] for identifier in sorted(view.loaded)]
        view.status = "ready"
        view.message = None

    def _public_view(self, view: _PoiView) -> dict[str, object]:
        places: list[dict[str, object]] = list(view.places)
        total = view.total_in_view
        loaded = len(view.places)
        has_more = view.has_more
        if view.status == "zoom":
            places, total, loaded, has_more = [], 0, 0, False
        elif view.status == "ready":
            # This zoom's proximity grid — not a business/institution filter —
            # chooses which general named records the fusion frame publishes.
            places = drive_general_named_pois(view.places, view.bounds.zoom, view.bounds)
            total = len(places)
            loaded = len(places)
            has_more = False if view.cache_complete else view.has_more
        result: dict[str, object] = {
            "status": view.status, "places": copy.deepcopy(places), "total_in_view": total,
            "stored_count": view.stored_count, "loaded_count": loaded, "has_more": has_more,
            "queried_at": view.queried_at, "refresh_seconds": int(self.refresh_seconds),
            "coverage": {"source": "OpenStreetMap", "bounded": True, "exhaustive": False, "bounds": view.bounds.public(),
                         "scope": "recent requested neighborhood only; not global coverage"},
        }
        if view.message:
            result["message"] = view.message
        return result

    def _expire(self, now: float) -> None:
        for key in [key for key, view in self._views.items() if now - view.last_seen > self.expiry_seconds]:
            self._views.pop(key, None)
