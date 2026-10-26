"""Mapping extension attached after immutable CherryPro source execution.

The original source is already executed by ``source_extension_runner`` in this
same process.  This module intentionally does not call any source stream,
uplink, hardware, TPM, or disabled ``if False`` entrypoint.  It creates only a
bounded application mapping loop around the existing safe marker, GeoNames, and
Maps-provider runtimes.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .fusion_sync import verify_software_sync
from .index_runtime import GLOBAL_INDEX_QUERY, IndexRuntime
from .maps_service import MapsService
from .marker_runtime import MarkerRuntime
from .nearby_runtime import NearbyRuntime
from .poi_runtime import POIRuntime
from .label_search import LabelSearch
from .source_gate import EXPECTED_SHA256, SOURCE_PATH

MAX_COMMAND_BYTES = 16_384
MAX_REPLY_BYTES = 8 * 1024 * 1024
MAX_QUEUE_ITEMS = 32
HEARTBEAT_SECONDS = 1.0
SOURCE_SECONDS = 1.0
INDEX_SECONDS = 1.0
PROVIDER_SCAN_SECONDS = 1.0
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,180}\Z")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_COUNTRY_CODE = re.compile(r"[A-Z]{2,8}\Z")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(value: object, *, maximum: int, fallback: str | None = None) -> str | None:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or any(ord(character) < 32 for character in cleaned):
        return fallback
    return cleaned


def _coordinate(value: object, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result) or not -maximum <= result <= maximum:
        return None
    return result


def _timestamp(value: object) -> str | None:
    return _text(value, maximum=64) if isinstance(value, str) else None


def _safe_marker(raw: object, *, name: str) -> dict[str, object]:
    """Copy only browser display fields; source/provider extras never escape."""
    source = raw if isinstance(raw, dict) else {}
    title = _text(source.get("title"), maximum=255, fallback=name) or name
    label = _text(source.get("label"), maximum=255, fallback=name) or name
    color = source.get("color")
    safe_color = color if isinstance(color, str) and _HEX.fullmatch(color) else "#467da6"
    popup_raw = source.get("popup")
    popup_source = popup_raw if isinstance(popup_raw, dict) else {}
    popup: dict[str, object] = {
        "heading": _text(popup_source.get("heading"), maximum=255, fallback=name) or name,
        "provenance": _text(popup_source.get("provenance"), maximum=255, fallback="Source provenance unavailable") or "Source provenance unavailable",
    }
    source_line = popup_source.get("source_line")
    if isinstance(source_line, int) and not isinstance(source_line, bool) and 0 < source_line <= 10_000_000:
        popup["source_line"] = source_line
    return {"title": title, "label": label, "color": safe_color, "popup": popup}


def _safe_cluster(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    count = raw.get("count")
    bounds = raw.get("bounds")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 236_560:
        return None
    if not isinstance(bounds, dict) or set(bounds) != {"south", "north", "west", "east"}:
        return None
    south, north = _coordinate(bounds["south"], maximum=90), _coordinate(bounds["north"], maximum=90)
    west, east = _coordinate(bounds["west"], maximum=180), _coordinate(bounds["east"], maximum=180)
    if south is None or north is None or west is None or east is None or not south < north:
        return None
    return {"count": count, "bounds": {"south": south, "north": north, "west": west, "east": east}}


def _safe_coverage(raw: object) -> dict[str, object] | None:
    """Allowlist the non-additive overview accounting object."""
    if not isinstance(raw, dict) or set(raw) != {
        "represented_records", "named_total", "named_loaded", "cluster_count", "min_population", "includes_capitals",
    }:
        return None
    numeric = ("represented_records", "named_total", "named_loaded", "cluster_count", "min_population")
    if any(
        isinstance(raw.get(key), bool) or not isinstance(raw.get(key), int) or int(raw[key]) < 0
        for key in numeric
    ):
        return None
    if int(raw["named_loaded"]) > int(raw["named_total"]) or raw["includes_capitals"] is not True:
        return None
    return {
        "represented_records": int(raw["represented_records"]),
        "named_total": int(raw["named_total"]),
        "named_loaded": int(raw["named_loaded"]),
        "cluster_count": int(raw["cluster_count"]),
        "min_population": int(raw["min_population"]),
        "includes_capitals": True,
    }


def _safe_record(raw: object, *, family: str, known_source_ids: set[str]) -> dict[str, object] | None:
    """Accept a known native shape and emit a narrow, fresh public record only."""
    if not isinstance(raw, dict):
        return None
    identifier, name = raw.get("id"), _text(raw.get("name"), maximum=255)
    latitude, longitude = _coordinate(raw.get("lat"), maximum=90), _coordinate(raw.get("lng"), maximum=180)
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or name is None or latitude is None or longitude is None:
        return None
    origin = raw.get("origin")
    if family == "source":
        if origin != "source" or identifier not in known_source_ids:
            return None
    elif family == "index":
        if origin not in {"open-index", "index-cluster"}:
            return None
        if origin == "open-index" and not identifier.startswith("geonames:"):
            return None
        if origin == "index-cluster" and not identifier.startswith("gncluster:"):
            return None
    elif family == "provider":
        if origin != "provider-worker":
            return None
    elif family == "poi":
        osm_place = origin == "open-poi" and (
            re.fullmatch(r"osm:(?:node|way|relation):[1-9][0-9]*", identifier) is not None
            or identifier.startswith("poicluster:")
        )
        neural_place = origin == "neural-feed" and identifier.startswith("neural:")
        if not osm_place and not neural_place:
            return None
    else:
        return None

    record: dict[str, object] = {
        "id": identifier, "name": name, "lat": latitude, "lng": longitude,
        "origin": str(origin), "marker": _safe_marker(raw.get("marker"), name=name),
    }
    allowed_text = {
        "address": 300, "category": 64, "position_origin": 80, "data_modified_at": 64,
        "refreshed_at": 64, "queried_at": 64, "observed_at": 64, "place_id": 180,
    }
    for field, maximum in allowed_text.items():
        value = _text(raw.get(field), maximum=maximum)
        if value is not None:
            record[field] = value
    if family == "poi":
        # Keep optional OSM tag fields flat for browser tooltips. With the core
        # marker fields this remains under the supervisor's 16-key hard limit.
        for key in ("website", "opening_hours", "brand"):
            clean = _text(raw.get(key), maximum=255)
            if clean is not None:
                record[key] = clean
    source_line = raw.get("source_line")
    if family == "source" and isinstance(source_line, int) and not isinstance(source_line, bool) and 0 < source_line <= 10_000_000:
        record["source_line"] = source_line
    if family == "index" and origin == "open-index":
        population = raw.get("population")
        country_code = raw.get("country_code")
        overview = raw.get("overview")
        if (
            isinstance(population, bool) or not isinstance(population, int) or population < 0
            or not isinstance(country_code, str) or not _COUNTRY_CODE.fullmatch(country_code)
            or not isinstance(overview, bool)
        ):
            return None
        # A genuine native GeoNames placemark has 13 public keys at present,
        # safely below the supervisor's fixed 16-key boundary check.
        record["population"] = population
        record["country_code"] = country_code
        record["overview"] = overview
    if family == "index" and origin == "index-cluster":
        cluster = _safe_cluster(raw.get("cluster"))
        if cluster is None:
            return None
        record["cluster"] = cluster
    if family == "poi" and raw.get("cluster") is not None:
        cluster = _safe_cluster(raw.get("cluster"))
        if cluster is None:
            return None
        record["cluster"] = cluster
    return record


def _accepted(records: object, *, family: str, known_source_ids: set[str]) -> list[dict[str, object]]:
    """Reject invalid/duplicate collection entries before any count is reported."""
    if not isinstance(records, list):
        return []
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in records:
        record = _safe_record(raw, family=family, known_source_ids=known_source_ids)
        if record is None or record["id"] in seen:
            continue
        seen.add(str(record["id"]))
        result.append(record)
    return result


def fuse_markers(source: object, index: object, provider: object, poi: object | None = None) -> dict[str, object]:
    """Fuse accepted records by exact identifier only, never coordinate proximity."""
    source_raw = source if isinstance(source, list) else []
    # The AST verified source snapshot is authoritative; do not infer IDs from
    # arbitrary namespace dictionaries or instantiate original source classes.
    source_ids = {
        str(item.get("id")) for item in source_raw
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    source_records = _accepted(source_raw, family="source", known_source_ids=source_ids)
    index_records = _accepted(index, family="index", known_source_ids=source_ids)
    provider_records = _accepted(provider, family="provider", known_source_ids=source_ids)
    poi_records = _accepted(poi, family="poi", known_source_ids=source_ids)
    fused: dict[str, dict[str, object]] = {}
    for collection in (source_records, index_records, provider_records, poi_records):
        for record in collection:
            # Source first establishes precedence only on *the same exact ID*.
            fused.setdefault(str(record["id"]), record)
    return {
        "markers": list(fused.values()),
        "fused_at": _now(),
        "counts": {
            "source": len(source_records), "index": len(index_records), "provider": len(provider_records),
            "total": len(fused),
            **({"poi": len(poi_records)} if poi is not None else {}),
        },
        "policy": "source-authoritative; preserve distinct index/provider provenance",
        "data_mode": "actual source-defined coordinates plus configured GeoNames index/provider data; no sensor feed configured; TPM status unverified",
    }


def _hidden_nearby(runtime: NearbyRuntime) -> dict[str, object]:
    return {
        "status": "hidden", "places": [], "has_more": False, "last_refresh_at": None,
        "refresh_seconds": int(runtime.refresh_seconds), "worker_running": bool(runtime.worker_running),
        "coverage": {"radius_meters": None, "result_limit": 20, "exhaustive": False},
    }


def _normalise_query(value: object) -> dict[str, object] | None:
    """Validate a complete bounded query locally; no arbitrary instructions cross IPC."""
    if not isinstance(value, dict):
        return None
    allowed = {"nearby", "index", "poi", "lat", "lng", "radius", "south", "north", "west", "east", "zoom"}
    if set(value) - allowed:
        return None
    if not value:
        return {}
    nearby, index = value.get("nearby", False), value.get("index", False)
    # Existing index viewport requests gain the local POI cache automatically;
    # low zoom produces ``zoom`` and never causes public Overpass traffic.
    poi = value.get("poi", index)
    if not isinstance(nearby, bool) or not isinstance(index, bool) or not isinstance(poi, bool):
        return None
    query: dict[str, object] = {"nearby": nearby, "index": index, "poi": poi}
    if nearby:
        lat, lng, radius = value.get("lat"), value.get("lng"), value.get("radius")
        lat_value, lng_value = _coordinate(lat, maximum=90), _coordinate(lng, maximum=180)
        if lat_value is None or lng_value is None or isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 5000:
            return None
        query.update({"lat": lat_value, "lng": lng_value, "radius": radius})
    if index or poi:
        south, north = _coordinate(value.get("south"), maximum=90), _coordinate(value.get("north"), maximum=90)
        west, east, zoom = _coordinate(value.get("west"), maximum=180), _coordinate(value.get("east"), maximum=180), value.get("zoom")
        if south is None or north is None or west is None or east is None or not south < north or west == east:
            return None
        if isinstance(zoom, bool) or not isinstance(zoom, int) or not 2 <= zoom <= 20:
            return None
        query.update({"south": south, "north": north, "west": west, "east": east, "zoom": zoom})
    return query


def snapshot(marker: MarkerRuntime, index_runtime: IndexRuntime, nearby_runtime: NearbyRuntime, poi_runtime: POIRuntime, query: object, runner: dict[str, object]) -> dict[str, object]:
    """Construct a wholly sanitized public frame from currently cached data."""
    normalised: dict[str, Any] = _normalise_query(query) or {}
    # Ready/heartbeat/default bootstrap frames retain the actual worldwide
    # overview rather than replacing it with a source-only hidden-index cache.
    # Explicit non-empty client queries retain their exact bounded viewport.
    if not normalised:
        normalised = copy.deepcopy(GLOBAL_INDEX_QUERY)
    source_records = _accepted(marker.locations(), family="source", known_source_ids={str(item.get("id")) for item in marker.locations() if isinstance(item, dict)})
    source_ids = {str(item["id"]) for item in source_records}
    if normalised.get("nearby") is True:
        nearby_raw = nearby_runtime.snapshot_for_view(
            lat=float(normalised["lat"]), lng=float(normalised["lng"]), radius=int(normalised["radius"])
        )
    else:
        nearby_raw = _hidden_nearby(nearby_runtime)
    if normalised.get("index") is True:
        index_raw = index_runtime.snapshot_for_view(
            south=float(normalised["south"]), north=float(normalised["north"]), west=float(normalised["west"]),
            east=float(normalised["east"]), zoom=int(normalised["zoom"])
        )
    else:
        index_raw = index_runtime.snapshot()
    if normalised.get("poi") is True:
        poi_raw = poi_runtime.snapshot_for_view(
            south=float(normalised["south"]), north=float(normalised["north"]), west=float(normalised["west"]),
            east=float(normalised["east"]), zoom=int(normalised["zoom"])
        )
    else:
        poi_raw = poi_runtime.hidden_snapshot()
    # Preserve public runtime status metadata but replace each place collection
    # with independently allowlisted records.
    safe_nearby = {
        key: copy.deepcopy(value) for key, value in nearby_raw.items()
        if key in {"status", "has_more", "last_refresh_at", "refresh_seconds", "worker_running", "coverage", "message"}
    }
    safe_index = {
        key: copy.deepcopy(value) for key, value in index_raw.items()
        if key in {"status", "dataset", "worker_running", "refreshed_at", "total_in_view", "loaded_count", "has_more", "mode", "message"}
    }
    safe_poi = {
        key: copy.deepcopy(value) for key, value in poi_raw.items()
        if key in {"status", "total_in_view", "stored_count", "loaded_count", "has_more", "queried_at", "refresh_seconds", "coverage", "message"}
    }
    if index_raw.get("mode") == "overview":
        coverage = _safe_coverage(index_raw.get("coverage"))
        if coverage is not None:
            safe_index["coverage"] = coverage
    nearby_candidates = list(nearby_raw.get("places") or []) if isinstance(nearby_raw.get("places"), list) else []
    # Selected details live in the child for five minutes and are deliberately
    # fused into each 1-second frame. Exact IDs prevent duplicate nearby rows.
    nearby_candidates.extend(nearby_runtime.selected_places())
    nearby_records = _accepted(nearby_candidates, family="provider", known_source_ids=source_ids)
    index_records = _accepted(index_raw.get("places"), family="index", known_source_ids=source_ids)
    poi_records = _accepted(poi_raw.get("places"), family="poi", known_source_ids=source_ids)
    safe_nearby["places"] = nearby_records
    safe_index["places"] = index_records
    safe_poi["places"] = poi_records
    worker = marker.public_worker()
    worker["runner"] = copy.deepcopy(runner)
    worker["sensor_data"] = "not configured"
    worker["tpm_status"] = "unverified"
    worker["tpm_bridge"] = {"mode": "virtual-interface", "hardware_verified": False, "status": "software-only"}
    fusion = fuse_markers(source_records, index_records, nearby_records, poi_records)
    worker["sync"] = verify_software_sync(
        source=source_records, worker=worker, index=safe_index,
        nearby=safe_nearby, fusion=fusion, poi=safe_poi, runner=runner,
    )
    return {
        "locations": source_records,
        "worker": worker,
        "nearby": safe_nearby,
        "index": safe_index,
        "poi": safe_poi,
        "fusion": fusion,
    }


def _emit(protocol_stdout: Any, message: dict[str, object]) -> bool:
    try:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(encoded) > MAX_REPLY_BYTES:
        return False
    protocol_stdout.write(encoded.decode("utf-8") + "\n")
    protocol_stdout.flush()
    return True


async def _read_commands(reader: asyncio.StreamReader, queue: asyncio.Queue[dict[str, object]], stop: asyncio.Event) -> None:
    """Read bounded line frames without executor threads that could hang shutdown."""
    try:
        while not stop.is_set():
            try:
                raw = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # Oversized input is intentionally not a command; the stream is
                # closed to avoid parsing an arbitrary remainder.
                stop.set()
                return
            if not raw:
                stop.set()
                return
            if len(raw) > MAX_COMMAND_BYTES:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            operation = message.get("op")
            if operation == "stop" and set(message) == {"op"}:
                await queue.put(message)
            elif operation == "snapshot" and set(message) == {"op", "request_id", "query"}:
                request_id = message.get("request_id")
                if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", request_id) and _normalise_query(message.get("query")) is not None:
                    if not queue.full():
                        queue.put_nowait({"op": "snapshot", "request_id": request_id, "query": _normalise_query(message["query"]) or {}})
            elif operation == "place" and set(message) == {"op", "request_id", "place_id"}:
                request_id, place_id = message.get("request_id"), message.get("place_id")
                if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", request_id) and isinstance(place_id, str) and _ID.fullmatch(place_id):
                    if not queue.full():
                        queue.put_nowait({"op": "place", "request_id": request_id, "place_id": place_id})
            elif operation == "search" and set(message) == {"op", "request_id", "q", "scope", "lat", "lng", "radius"}:
                request_id, q, scope = message.get("request_id"), message.get("q"), message.get("scope")
                if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", request_id) and isinstance(q, str) and isinstance(scope, str):
                    if not queue.full():
                        queue.put_nowait({"op": "search", "request_id": request_id, "q": q, "scope": scope, "lat": message.get("lat"), "lng": message.get("lng"), "radius": message.get("radius")})
    except asyncio.CancelledError:
        raise


async def _refresh_task(name: str, runtime: object) -> str:
    try:
        await runtime.refresh_once()  # type: ignore[attr-defined]
        return name
    except asyncio.CancelledError:
        raise
    except Exception:
        # Runtime implementations already retain their last valid snapshots.
        return name


async def _place_reply(marker: MarkerRuntime, nearby_runtime: NearbyRuntime, request_id: str, place_id: str, protocol_stdout: Any) -> None:
    """Resolve a selected, already-visible provider ID without delaying heartbeats."""
    try:
        place = await nearby_runtime.place_details(place_id)
    except Exception:
        place = None
    place = _safe_record(place, family="provider", known_source_ids=set()) if place is not None else None
    source_locations = marker.locations()
    source_records = _accepted(source_locations, family="source", known_source_ids={str(item.get("id")) for item in source_locations if isinstance(item, dict)})
    fusion = fuse_markers(source_records, [], [place] if place is not None else [])
    _emit(protocol_stdout, {"type": "place-reply", "request_id": request_id, "place": place, "fusion": fusion, "worker": marker.public_worker()})


async def _search_reply(searcher: LabelSearch, command: dict[str, object], stdout: Any) -> None:
    request_id = str(command["request_id"])
    try:
        result = await asyncio.to_thread(searcher.search, command.get("q"), lat=command.get("lat"), lng=command.get("lng"), radius=command.get("radius"), scope=str(command.get("scope", "world")))
    except Exception:
        result = {"results": [], "query": str(command.get("q", "")), "scope": str(command.get("scope", "world")), "count": 0, "warnings": ["Search is temporarily unavailable. Wait briefly and retry."]}
    _emit(stdout, {"type": "search-reply", "request_id": request_id, "result": result})


async def _loop(marker: MarkerRuntime, index_runtime: IndexRuntime, nearby_runtime: NearbyRuntime, poi_runtime: POIRuntime, protocol_stdout: Any, runner: dict[str, object], label_search: LabelSearch) -> int:
    loop = asyncio.get_running_loop()
    stream_reader = asyncio.StreamReader(limit=MAX_COMMAND_BYTES + 1)
    stream_protocol = asyncio.StreamReaderProtocol(stream_reader)
    transport, _ = await loop.connect_read_pipe(lambda: stream_protocol, sys.stdin.buffer)
    command_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=MAX_QUEUE_ITEMS)
    stopped = asyncio.Event()
    reader_task = asyncio.create_task(_read_commands(stream_reader, command_queue, stopped), name="cherrymap-extension-ipc-reader")
    tasks: dict[str, asyncio.Task[str]] = {}
    detail_tasks: set[asyncio.Task[None]] = set()
    next_due = {"source": time.monotonic() + SOURCE_SECONDS, "index": time.monotonic() + INDEX_SECONDS, "provider": time.monotonic() + PROVIDER_SCAN_SECONDS, "poi": time.monotonic() + INDEX_SECONDS}
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    try:
        while not stopped.is_set():
            now = time.monotonic()
            for name, runtime, interval in (
                ("source", marker, SOURCE_SECONDS), ("index", index_runtime, INDEX_SECONDS), ("provider", nearby_runtime, PROVIDER_SCAN_SECONDS), ("poi", poi_runtime, INDEX_SECONDS),
            ):
                existing = tasks.get(name)
                if now >= next_due[name] and (existing is None or existing.done()):
                    tasks[name] = asyncio.create_task(_refresh_task(name, runtime), name=f"cherrymap-extension-{name}-tick")
                    next_due[name] = now + interval
            for name, task in tuple(tasks.items()):
                if task.done():
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    tasks.pop(name, None)
            if marker.fatal:
                _emit(protocol_stdout, {"type": "fatal", "category": "approved-source-verification-failed"})
                return 68
            # Process several queued requests even while DB/provider task(s) run.
            for _ in range(12):
                try:
                    command = command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if command.get("op") == "stop":
                    stopped.set()
                    break
                request_id = command.get("request_id")
                if command.get("op") == "snapshot" and isinstance(request_id, str):
                    frame = snapshot(marker, index_runtime, nearby_runtime, poi_runtime, command.get("query"), runner)
                    if not _emit(protocol_stdout, {"type": "reply", "request_id": request_id, "snapshot": frame}):
                        _emit(protocol_stdout, {"type": "fatal", "category": "public-frame-too-large"})
                        return 69
                elif command.get("op") == "place" and isinstance(request_id, str) and isinstance(command.get("place_id"), str):
                    if len(detail_tasks) >= 2:
                        _emit(protocol_stdout, {"type": "place-reply", "request_id": request_id, "place": None, "fusion": fuse_markers(marker.locations(), [], []), "worker": marker.public_worker()})
                    else:
                        task = asyncio.create_task(_place_reply(marker, nearby_runtime, request_id, str(command["place_id"]), protocol_stdout), name="cherrymap-extension-place-detail")
                        detail_tasks.add(task)
                        task.add_done_callback(detail_tasks.discard)
                elif command.get("op") == "search" and isinstance(request_id, str):
                    if len(detail_tasks) >= 2:
                        _emit(protocol_stdout, {"type": "search-reply", "request_id": request_id, "result": {"results": [], "query": str(command.get("q", "")), "scope": str(command.get("scope", "world")), "count": 0, "warnings": ["Search is temporarily unavailable. Wait briefly and retry."]}})
                    else:
                        task = asyncio.create_task(_search_reply(label_search, command, protocol_stdout), name="cherrymap-source-label-search")
                        detail_tasks.add(task)
                        task.add_done_callback(detail_tasks.discard)
            if now >= next_heartbeat:
                frame = snapshot(marker, index_runtime, nearby_runtime, poi_runtime, {}, runner)
                if not _emit(protocol_stdout, {"type": "heartbeat", "snapshot": frame}):
                    _emit(protocol_stdout, {"type": "fatal", "category": "public-frame-too-large"})
                    return 69
                next_heartbeat = now + HEARTBEAT_SECONDS
            await asyncio.sleep(0.025)
        return 0
    finally:
        stopped.set()
        reader_task.cancel()
        for task in tasks.values():
            task.cancel()
        for task in detail_tasks:
            task.cancel()
        await asyncio.gather(reader_task, *tasks.values(), *detail_tasks, return_exceptions=True)
        transport.close()


def run(*, namespace: dict[str, object], config: dict[str, str], protocol_stdout: Any, runner: dict[str, object], private_log: Any) -> int:
    """Attach to the already-executed namespace, then own the continuous loop."""
    # ``namespace`` is deliberately accepted only as execution proof. Extension
    # data comes from the independently hash-checked AST extractor, not arbitrary
    # source globals or the source's dormant networking/hardware helpers.
    del namespace
    try:
        marker = MarkerRuntime(source_path=SOURCE_PATH, expected_sha256=EXPECTED_SHA256, updates_path=Path(config["updates_path"]), interval_seconds=SOURCE_SECONDS)
        index_runtime = IndexRuntime(refresh_seconds=INDEX_SECONDS, scan_seconds=INDEX_SECONDS)
        nearby_runtime = NearbyRuntime(MapsService(api_url=config["maps_api_url"], api_key=config["maps_api_key"]))
        poi_runtime = POIRuntime()
        label_search = LabelSearch(nearby_runtime.maps_service, marker, nearby_runtime)

        async def main() -> int:
            await marker.initialize()
            if marker.fatal:
                _emit(protocol_stdout, {"type": "fatal", "category": "approved-source-verification-failed"})
                return 68
            await index_runtime.initialize()
            await nearby_runtime.initialize()
            # Additive POI migration completes (or records an honest local
            # availability error) before the browser receives its first frame.
            await poi_runtime.initialize()
            # Register and perform one bounded low-zoom read before ready. If
            # metadata is unavailable, IndexRuntime emits its honest error view
            # without making the verified original source worker fatal.
            index_runtime.snapshot_for_view(
                south=float(GLOBAL_INDEX_QUERY["south"]), north=float(GLOBAL_INDEX_QUERY["north"]),
                west=float(GLOBAL_INDEX_QUERY["west"]), east=float(GLOBAL_INDEX_QUERY["east"]),
                zoom=int(GLOBAL_INDEX_QUERY["zoom"]),
            )
            await index_runtime.refresh_once()
            initial = snapshot(marker, index_runtime, nearby_runtime, poi_runtime, GLOBAL_INDEX_QUERY, runner)
            if not _emit(protocol_stdout, {"type": "ready", "snapshot": initial}):
                return 69
            try:
                return await _loop(marker, index_runtime, nearby_runtime, poi_runtime, protocol_stdout, runner, label_search)
            finally:
                await nearby_runtime.stop(source_fatal=marker.fatal)
                await poi_runtime.stop(source_fatal=marker.fatal)
                await index_runtime.stop(source_fatal=marker.fatal)
                await marker.stop()

        return asyncio.run(main())
    except BaseException as exc:
        # Keep a bounded private category only; no source/config/exception text is
        # written to stdout or browser-visible status.
        try:
            import traceback
            detail = f"mapping extension failure {type(exc).__name__}: {exc}\n{traceback.format_exc()[-4000:]}"
            private_log.write(detail)
            failure_path = Path(private_log._file.name).with_name("extension-failure.log")
            with open(failure_path, "w", encoding="utf-8") as handle:
                handle.write(detail)
        except Exception:
            pass
        _emit(protocol_stdout, {"type": "fatal", "category": "mapping-extension-failed"})
        return 67
