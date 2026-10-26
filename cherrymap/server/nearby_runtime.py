"""Bounded, app-owned refreshes for active nearby-map views.

This runtime never receives an HTTP request object or browser credentials.  It is
started and stopped by :class:`server.marker_runtime.MarkerRuntime` only after
the immutable-source startup gate and initial source snapshot have succeeded.
"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from collections import OrderedDict
from functools import wraps
from threading import RLock
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import HTTPException

from .maps_service import MapsService

DEFAULT_REFRESH_SECONDS = 60.0
DEFAULT_SCAN_SECONDS = 1.0
DEFAULT_EXPIRY_SECONDS = 90.0
MAX_ACTIVE_VIEWS = 8
RESULT_LIMIT = 60
MAX_CONCURRENT_FETCHES = 2
PROVIDER_PROVENANCE = "Map-provider nearby place — refreshed by Python worker"
PROVIDER_LOOKUP_TTL_SECONDS = 300.0
MAX_PROVIDER_LOOKUPS = 32


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _provider_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lookup_lock:
            return method(self, *args, **kwargs)
    return wrapped


@dataclass
class _NearbyView:
    key: str
    lat: float
    lng: float
    radius: int
    last_seen: float
    next_refresh_at: float
    status: str = "loading"
    places: list[dict[str, object]] | None = None
    has_more: bool = False
    last_refresh_at: str | None = None
    message: str | None = None


class NearbyRuntime:
    """Refresh at most eight recently requested, quantized nearby views.

    A route merely registers a bounded view and immediately reads its cached
    state.  All provider I/O is performed by this task at the slower provider
    interval, using ``MapsService.nearby_for_worker(..., force_refresh=True)``
    so a newly stamped worker response can never be an old 300-second cache
    entry.  A failed refresh keeps the last successful records atomically.
    """

    def __init__(
        self,
        maps_service: MapsService,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        scan_seconds: float = DEFAULT_SCAN_SECONDS,
        expiry_seconds: float = DEFAULT_EXPIRY_SECONDS,
        max_active_views: int = MAX_ACTIVE_VIEWS,
        max_concurrent_fetches: int = MAX_CONCURRENT_FETCHES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if refresh_seconds <= 0 or scan_seconds <= 0 or expiry_seconds <= 0:
            raise ValueError("nearby worker intervals must be positive")
        if max_active_views < 1 or max_concurrent_fetches < 1:
            raise ValueError("nearby worker limits must be positive")
        self.maps_service = maps_service
        self.refresh_seconds = float(refresh_seconds)
        self.scan_seconds = float(scan_seconds)
        self.expiry_seconds = float(expiry_seconds)
        self.max_active_views = int(max_active_views)
        self._clock = clock
        self._views: dict[str, _NearbyView] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._externally_driven = False
        self._terminal_error: str | None = None
        self._source_failed = False
        self._fetches = asyncio.Semaphore(int(max_concurrent_fetches))
        # This is not a provider/global directory. It contains at most 32 IDs
        # that were actually returned for current recent map views, for 5 min.
        self._lookup_lock = RLock()
        self._provider_lookup: OrderedDict[str, tuple[float, dict[str, object]]] = OrderedDict()

    @property
    def ready(self) -> bool:
        """True only while the lifecycle owner still has a live child task."""
        return self._running and (
            self._externally_driven or (self._task is not None and not self._task.done())
        )

    @property
    def worker_running(self) -> bool:
        return self.ready

    async def start(self) -> None:
        """Start the lightweight scan loop; it does no I/O until a view exists."""
        if self._task is not None or self._running:
            return
        self._running = True
        self._externally_driven = False
        self._terminal_error = None
        self._source_failed = False
        self._task = asyncio.create_task(self._run(), name="cherrymap-nearby-worker")

    async def initialize(self) -> None:
        """Mark this cache ready without creating its legacy private loop."""
        if self._task is not None:
            raise RuntimeError("nearby runtime already owns a refresh task")
        if self._running:
            return
        self._running = True
        self._externally_driven = True
        self._terminal_error = None
        self._source_failed = False

    async def stop(self, *, source_fatal: bool = False) -> None:
        """Cancel the child task without retaining an in-flight HTTP request."""
        self._running = False
        self._externally_driven = False
        self._source_failed = self._source_failed or source_fatal
        if source_fatal:
            self._terminal_error = "nearby worker stopped because source worker failed"
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def hidden_snapshot() -> dict[str, object]:
        """Return the required disabled-view contract without touching views."""
        return {
            "status": "hidden",
            "places": [],
            "has_more": False,
            "last_refresh_at": None,
            "refresh_seconds": int(DEFAULT_REFRESH_SECONDS),
            "worker_running": False,
            "coverage": {"radius_meters": None, "result_limit": RESULT_LIMIT, "exhaustive": False},
        }

    def register_view(self, *, lat: float, lng: float, radius: int) -> dict[str, object]:
        """Register/update a quantized view and return its current cached snapshot.

        This method intentionally performs no network request.  The request
        handler therefore remains fast even while provider calls are slow.
        """
        self._validate_view(lat, lng, radius)
        now = self._clock()
        self._expire(now)
        key = self._view_key(lat, lng, radius)
        view = self._views.get(key)
        if view is None:
            if len(self._views) >= self.max_active_views:
                # The cap is strict.  Prefer the least recently visible view,
                # which makes a newly visible viewport usable without retaining
                # an unbounded client list.
                oldest_key = min(self._views, key=lambda item: self._views[item].last_seen)
                self._views.pop(oldest_key, None)
            view = _NearbyView(
                key=key,
                lat=round(float(lat), 4),
                lng=round(float(lng), 4),
                radius=int(radius),
                last_seen=now,
                next_refresh_at=now,
                places=[],
            )
            self._views[key] = view
        else:
            view.last_seen = now
        return self._public_view(view)

    def snapshot_for_view(self, *, lat: float, lng: float, radius: int) -> dict[str, object]:
        """Alias with an explicit route-facing name for registering a view."""
        return self.register_view(lat=lat, lng=lng, radius=radius)

    def active_view_count(self) -> int:
        """Small test/diagnostic helper; no route exposes client view identities."""
        self._expire(self._clock())
        return len(self._views)

    async def refresh_once(self) -> None:
        """Refresh currently due views once (used by the scan task and tests)."""
        if not self._running:
            return
        now = self._clock()
        self._expire(now)
        due = [
            (key, view)
            for key, view in self._views.items()
            if view.next_refresh_at <= now
        ]
        if due:
            await asyncio.gather(
                *(self._refresh_view(key, view) for key, view in due),
            )

    async def _run(self) -> None:
        try:
            while self._running:
                await self.refresh_once()
                await asyncio.sleep(self.scan_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose exception text, endpoint URLs, or credentials.
            self._terminal_error = "nearby worker stopped unexpectedly"
            self._running = False
        finally:
            if not self._running and self._terminal_error is None:
                # Normal shutdown needs no public error, but a request cannot
                # falsely report a running worker after this task is gone.
                pass
            elif self._task is not None and self._task.done():
                self._running = False

    async def _refresh_view(self, key: str, view: _NearbyView) -> None:
        """Fetch one view through a bounded worker thread and publish atomically."""
        async with self._fetches:
            if not self._running or self._views.get(key) is not view:
                return
            attempted_at = _utc_now()
            try:
                result = await asyncio.to_thread(
                    self.maps_service.nearby_for_worker,
                    view.lat,
                    view.lng,
                    view.radius,
                    force_refresh=True,
                )
                places, has_more = self._enrich_provider_result(result, attempted_at)
            except Exception:
                # ``MapsService`` already guards provider parsing; this fixed
                # message ensures no exception detail can reveal a URL/key.
                if self._views.get(key) is view:
                    view.status = "error"
                    view.message = "nearby provider is unavailable"
                    view.last_refresh_at = attempted_at
                    view.next_refresh_at = self._clock() + self.refresh_seconds
                return

            if self._views.get(key) is not view:
                return
            view.places = places
            view.has_more = has_more
            view.status = "ready"
            view.message = None
            view.last_refresh_at = attempted_at
            # Start the 60-second cadence after a successful provider response,
            # not from a stale cache timestamp or the client request time.
            view.next_refresh_at = self._clock() + self.refresh_seconds

    def _enrich_provider_result(self, result: object, refreshed_at: str) -> tuple[list[dict[str, object]], bool]:
        if not isinstance(result, dict):
            raise ValueError("invalid worker provider response")
        raw_places = result.get("results")
        if not isinstance(raw_places, list):
            raise ValueError("invalid worker provider response")
        places: list[dict[str, object]] = []
        for raw_place in raw_places[:RESULT_LIMIT]:
            if not isinstance(raw_place, dict):
                continue
            # MapsService has already sanitized these fields.  Copy only its
            # public contract; never pass raw provider keys into marker state.
            required = ("id", "name", "address", "lat", "lng")
            if any(field not in raw_place for field in required):
                continue
            identifier = raw_place["id"]
            name = raw_place["name"]
            address = raw_place["address"]
            lat = raw_place["lat"]
            lng = raw_place["lng"]
            if (
                not isinstance(identifier, str)
                or not isinstance(name, str)
                or not isinstance(address, str)
                or isinstance(lat, bool)
                or isinstance(lng, bool)
                or not isinstance(lat, (int, float))
                or not isinstance(lng, (int, float))
                or not math.isfinite(float(lat))
                or not math.isfinite(float(lng))
                or not -90 <= float(lat) <= 90
                or not -180 <= float(lng) <= 180
            ):
                continue
            place: dict[str, object] = {
                "id": identifier,
                "name": name,
                "address": address,
                "lat": float(lat),
                "lng": float(lng),
                # Explicitly distinguish these managed-provider records from
                # the immutable Python source marker definitions.
                "origin": "provider-worker",
                "position_origin": "provider-nearby-worker",
                "refreshed_at": refreshed_at,
                "marker": {
                    "title": name,
                    "label": name,
                    "color": "#356d63",
                    "popup": {
                        "heading": name,
                        "provenance": PROVIDER_PROVENANCE,
                    },
                },
            }
            for optional in ("place_id", "category"):
                if optional in raw_place:
                    place[optional] = raw_place[optional]
            places.append(place)
            self._remember_provider_place(place)
        return places, bool(result.get("has_more"))

    @_provider_locked
    def _remember_provider_place(self, place: dict[str, object]) -> None:
        identifier = place.get("place_id") or place.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", identifier):
            return
        self._expire_provider_lookup(self._clock())
        self._provider_lookup[identifier] = (self._clock() + PROVIDER_LOOKUP_TTL_SECONDS, copy.deepcopy(place))
        self._provider_lookup.move_to_end(identifier)
        while len(self._provider_lookup) > MAX_PROVIDER_LOOKUPS:
            self._provider_lookup.popitem(last=False)

    @_provider_locked
    def selected_places(self) -> list[dict[str, object]]:
        """Return current short-lived selected/recent provider markers only."""
        self._expire_provider_lookup(self._clock())
        return [copy.deepcopy(record) for _, record in self._provider_lookup.values()]

    async def place_details(self, place_id: str) -> dict[str, object] | None:
        """Read a validated basemap POI ID, not only IDs in a capped nearby page."""
        if not isinstance(place_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", place_id):
            return None
        self._expire_provider_lookup(self._clock())
        with self._lookup_lock:
            entry = self._provider_lookup.get(place_id)
        known = entry[1] if entry else None
        try:
            details = await asyncio.to_thread(self.maps_service.place_details_for_worker, place_id)
        except Exception:
            return copy.deepcopy(known) if known is not None else None
        if not isinstance(details, dict):
            return copy.deepcopy(known) if known is not None else None
        name, lat, lng, address = (details.get(key) for key in ("name", "lat", "lng", "address"))
        if not isinstance(name, str) or not name or isinstance(lat, bool) or isinstance(lng, bool) or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or not math.isfinite(float(lat)) or not math.isfinite(float(lng)) or not -90 <= lat <= 90 or not -180 <= lng <= 180 or not isinstance(address, str):
            return copy.deepcopy(known) if known is not None else None
        enriched: dict[str, object] = {
            "id": place_id, "name": name, "lat": float(lat), "lng": float(lng), "address": address,
            "origin": "provider-worker", "place_id": place_id,
            "position_origin": "provider-nearby-worker",
            "refreshed_at": _utc_now(),
            "marker": {"title": name, "label": name, "color": "#356d63", "popup": {"heading": name, "provenance": PROVIDER_PROVENANCE}},
        }
        for optional in ("category", "business_status"):
            value = details.get(optional)
            if isinstance(value, str): enriched[optional] = value
        self._remember_provider_place(enriched)
        return enriched

    @_provider_locked
    def _expire_provider_lookup(self, now: float) -> None:
        for identifier in [key for key, (expires, _) in self._provider_lookup.items() if expires <= now]:
            self._provider_lookup.pop(identifier, None)

    def _public_view(self, view: _NearbyView) -> dict[str, object]:
        running = self.worker_running
        status = view.status
        message = view.message
        if not running:
            status = "error"
            message = self._terminal_error or "nearby worker is not running"
        last_refresh_at = view.last_refresh_at
        places = view.places or []
        has_more = bool(view.has_more)
        # No external place provider is configured. An endless loading/error
        # state was holding the whole fusion frame in "waiting".
        if running and not self.maps_service.provider_configured and not places:
            status = "ready"
            message = None
            has_more = False
            last_refresh_at = _utc_now()
        result: dict[str, object] = {
            "status": status,
            "places": copy.deepcopy(places),
            "has_more": has_more,
            "last_refresh_at": last_refresh_at,
            "refresh_seconds": int(self.refresh_seconds),
            "worker_running": running,
            "coverage": {
                "radius_meters": view.radius,
                "result_limit": RESULT_LIMIT,
                "exhaustive": False,
            },
        }
        if message:
            result["message"] = message
        return result

    def _expire(self, now: float) -> None:
        expired = [key for key, view in self._views.items() if now - view.last_seen > self.expiry_seconds]
        for key in expired:
            self._views.pop(key, None)

    @staticmethod
    def _view_key(lat: float, lng: float, radius: int) -> str:
        return f"{float(lat):.4f}:{float(lng):.4f}:{int(radius)}"

    @staticmethod
    def _validate_view(lat: object, lng: object, radius: object) -> None:
        if (
            isinstance(lat, bool)
            or isinstance(lng, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lng, (int, float))
            or not math.isfinite(float(lat))
            or not math.isfinite(float(lng))
            or not -90 <= float(lat) <= 90
            or not -180 <= float(lng) <= 180
        ):
            raise HTTPException(status_code=422, detail="lat and lng must be finite geographic coordinates")
        if isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 5000:
            raise HTTPException(status_code=422, detail="radius must be an integer between 100 and 5000")
