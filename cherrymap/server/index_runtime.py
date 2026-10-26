"""Application-owned runtime for the imported GeoNames cities500 index.

HTTP handlers only register/read bounded cached view state. Database reads occur in
at most two ``asyncio.to_thread`` workers owned by this runtime, after the
approved source gate and first source-marker snapshot have completed.
"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable, cast

from fastapi import HTTPException

from .geo_index import GeoIndexRepository, public_dataset_metadata, utc_now

DISPLAY_REFRESH_SECONDS = 5.0
METADATA_REFRESH_SECONDS = 30.0
SCAN_SECONDS = 0.25
EXPIRY_SECONDS = 90.0
MAX_ACTIVE_VIEWS = 8
MAX_CONCURRENT_QUERIES = 2
PLACE_PAGE_SIZE = 500
INDEX_PROVENANCE = "GeoNames · CC BY 4.0 · own geographic index"

# The world frame is registered and primed by the attached source-extension
# process before it emits its first ready frame.  This is a geographic index,
# not a provider result set, so it is intentionally independent of the Maps
# provider's 20-result nearby page.
GLOBAL_INDEX_QUERY: dict[str, object] = {
    "nearby": False,
    "index": True,
    "south": -90.0,
    "north": 90.0,
    "west": -180.0,
    "east": 180.0,
    "zoom": 2,
}

# Fixed GeoNames population LOD. PPLC country capitals are additionally kept
# at every overview zoom.  This is a documented display-selection rule, not an
# arbitrary provider-like result cap.  Coverage clusters still represent every
# geo_places row in the requested viewport.
OVERVIEW_POPULATION_BY_ZOOM: dict[int, int] = {
    2: 1_000_000,
    3: 500_000,
    4: 250_000,
    5: 100_000,
    6: 50_000,
    7: 10_000,
    8: 5_000,
    9: 500,
}
_COUNTRY_CODE = re.compile(r"[A-Z]{2,8}\Z")


@dataclass(frozen=True)
class IndexBounds:
    south: float
    north: float
    west: float
    east: float
    zoom: int


@dataclass
class _IndexView:
    key: str
    bounds: IndexBounds
    last_seen: float
    next_refresh_at: float
    status: str = "loading"
    places: list[dict[str, object]] = field(default_factory=list)
    total_in_view: int = 0
    cursor: int = 0
    loaded_ids: dict[int, dict[str, object]] = field(default_factory=dict)
    has_more: bool = False
    refresh_cursor: int = 0
    refresh_ids: dict[int, dict[str, object]] = field(default_factory=dict)
    refreshed_at: str | None = None
    message: str | None = None

    # Overview state is deliberately distinct from the native detail page:
    # ``coverage_clusters`` includes every geo_places row; ``loaded_ids`` has
    # only named LOD records and may overlap those grid cells without changing
    # the coverage total.
    overview_min_population: int = 0
    coverage_clusters: list[dict[str, object]] = field(default_factory=list)
    coverage_complete: bool = False
    named_total: int = 0
    named_count_known: bool = False
    overview_initialized: bool = False
    overview_refresh_started: bool = False
    overview_refresh_clusters: list[dict[str, object]] = field(default_factory=list)
    overview_refresh_total: int = 0
    overview_refresh_named_total: int = 0
    overview_refresh_ids: dict[int, dict[str, object]] = field(default_factory=dict)
    overview_refresh_cursor: int = 0


class IndexRuntime:
    """Cache up to eight recent map views without any DB work in request paths."""

    def __init__(
        self,
        repository: GeoIndexRepository | None = None,
        *,
        refresh_seconds: float = DISPLAY_REFRESH_SECONDS,
        metadata_refresh_seconds: float = METADATA_REFRESH_SECONDS,
        scan_seconds: float = SCAN_SECONDS,
        expiry_seconds: float = EXPIRY_SECONDS,
        max_active_views: int = MAX_ACTIVE_VIEWS,
        max_concurrent_queries: int = MAX_CONCURRENT_QUERIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(refresh_seconds, metadata_refresh_seconds, scan_seconds, expiry_seconds) <= 0:
            raise ValueError("index runtime intervals must be positive")
        if max_active_views < 1 or max_concurrent_queries < 1:
            raise ValueError("index runtime limits must be positive")
        self.repository = repository or GeoIndexRepository()
        self.refresh_seconds = float(refresh_seconds)
        self.metadata_refresh_seconds = float(metadata_refresh_seconds)
        self.scan_seconds = float(scan_seconds)
        self.expiry_seconds = float(expiry_seconds)
        self.max_active_views = int(max_active_views)
        self._clock = clock
        self._queries = asyncio.Semaphore(int(max_concurrent_queries))
        self._views: dict[str, _IndexView] = {}
        self._running = False
        self._externally_driven = False
        self._source_failed = False
        self._task: asyncio.Task[None] | None = None
        self._dataset = public_dataset_metadata(None)
        self._dataset_available = False
        self._metadata_message: str | None = None
        self._last_metadata_at = float("-inf")
        self._terminal_error: str | None = None

    @property
    def ready(self) -> bool:
        """The task can be healthy while an unimported database reports error views."""
        return self._running and (
            self._externally_driven or (self._task is not None and not self._task.done())
        )

    @property
    def worker_running(self) -> bool:
        return self.ready

    async def start(self) -> None:
        if self._task is not None or self._running:
            return
        self._running = True
        self._externally_driven = False
        self._source_failed = False
        self._terminal_error = None
        await self._refresh_metadata()
        self._task = asyncio.create_task(self._run(), name="cherrymap-geonames-index-worker")

    async def initialize(self) -> None:
        """Read metadata without scheduling a private scan task.

        The source-extension scheduler explicitly calls ``refresh_once``
        thereafter, keeping source/index/provider timing in one child process.
        """
        if self._task is not None:
            raise RuntimeError("index runtime already owns a refresh task")
        if self._running:
            return
        self._running = True
        self._externally_driven = True
        self._source_failed = False
        self._terminal_error = None
        await self._refresh_metadata()

    async def stop(self, *, source_fatal: bool = False) -> None:
        self._running = False
        self._externally_driven = False
        self._source_failed = self._source_failed or source_fatal
        if source_fatal:
            self._terminal_error = "index worker stopped because source worker failed"
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def hidden_snapshot(dataset: dict[str, object] | None = None, *, worker_running: bool = False) -> dict[str, object]:
        return {
            "status": "hidden",
            "places": [],
            "dataset": copy.deepcopy(dataset or public_dataset_metadata(None)),
            "worker_running": worker_running,
            "refreshed_at": None,
            "total_in_view": 0,
            "loaded_count": 0,
            "has_more": False,
            "mode": "overview",
        }

    def snapshot(self) -> dict[str, object]:
        """Disabled contract; it never creates or changes a retained view."""
        return self.hidden_snapshot(self._dataset, worker_running=self.worker_running)

    def snapshot_for_view(
        self, *, south: float, north: float, west: float, east: float, zoom: int
    ) -> dict[str, object]:
        """Register a view with no database I/O and return its present cache state."""
        bounds = self.validate_bounds(south=south, north=north, west=west, east=east, zoom=zoom)
        now = self._clock()
        self._expire(now)
        key = self._view_key(bounds)
        view = self._views.get(key)
        if view is None:
            if len(self._views) >= self.max_active_views:
                oldest = min(self._views, key=lambda item: self._views[item].last_seen)
                self._views.pop(oldest, None)
            view = _IndexView(
                key=key,
                bounds=bounds,
                last_seen=now,
                next_refresh_at=now,
                overview_min_population=self._overview_min_population(bounds.zoom) if bounds.zoom < 10 else 0,
            )
            self._views[key] = view
        else:
            view.last_seen = now
        return self._public_view(view)

    def active_view_count(self) -> int:
        self._expire(self._clock())
        return len(self._views)

    async def refresh_once(self) -> None:
        if not self._running:
            return
        now = self._clock()
        self._expire(now)
        if now - self._last_metadata_at >= self.metadata_refresh_seconds:
            await self._refresh_metadata()
        due = [(key, view) for key, view in self._views.items() if view.next_refresh_at <= now]
        if due:
            await asyncio.gather(*(self._refresh_view(key, view) for key, view in due))

    async def _run(self) -> None:
        try:
            while self._running:
                await self.refresh_once()
                await asyncio.sleep(self.scan_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._terminal_error = "index worker stopped unexpectedly"
            self._running = False

    async def _refresh_metadata(self) -> None:
        self._last_metadata_at = self._clock()
        try:
            result = await asyncio.to_thread(self.repository.dataset_metadata)
            dataset = result.get("dataset")
            if not isinstance(dataset, dict):
                raise ValueError("invalid index metadata")
            self._dataset = copy.deepcopy(dataset)
            self._dataset_available = bool(result.get("available"))
            message = result.get("message")
            self._metadata_message = str(message) if message else None
        except Exception:
            self._dataset_available = False
            self._metadata_message = "GeoNames index metadata is unavailable"

    async def _refresh_view(self, key: str, view: _IndexView) -> None:
        async with self._queries:
            if not self._running or self._views.get(key) is not view:
                return
            attempted_at = utc_now()
            if not self._dataset_available:
                if self._views.get(key) is view:
                    view.status = "error"
                    view.message = self._metadata_message or "GeoNames index is not imported"
                    view.refreshed_at = attempted_at
                    view.next_refresh_at = self._clock() + self.refresh_seconds
                return
            try:
                if view.bounds.zoom < 10:
                    await self._refresh_overview_view(view, attempted_at)
                else:
                    await self._refresh_places_view(view, attempted_at)
            except Exception:
                # Preserve the complete last valid page/cluster snapshot.
                if self._views.get(key) is view:
                    view.status = "error"
                    view.message = "GeoNames index query is unavailable"
                    view.refreshed_at = attempted_at
                    view.next_refresh_at = self._clock() + self.refresh_seconds

    async def _refresh_overview_view(self, view: _IndexView, attempted_at: str) -> None:
        """Refresh a low-zoom actual-name page plus all-record grid coverage.

        Named records and clusters deliberately overlap geographically.  The
        published ``total_in_view`` and ``coverage.represented_records`` are the
        sum of clusters only, never that sum plus named records.
        """
        if view.overview_initialized and not view.has_more:
            await self._refresh_completed_overview(view, attempted_at)
            return

        if not view.coverage_complete:
            clusters, total = await self._read_clusters(view)
            view.coverage_clusters = clusters
            view.total_in_view = total
            view.coverage_complete = True
        if not view.named_count_known:
            view.named_total = await asyncio.to_thread(
                self.repository.overview_count,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
                min_population=view.overview_min_population,
            )
            view.named_count_known = True
        if len(view.loaded_ids) < view.named_total:
            rows = await asyncio.to_thread(
                self.repository.overview_page,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
                min_population=view.overview_min_population,
                after_id=view.cursor,
                limit=PLACE_PAGE_SIZE,
            )
            self._append_markers(view.loaded_ids, rows, cursor_owner=view, refreshed_at=attempted_at, overview=True)
        view.places = self._overview_places(view)
        view.has_more = len(view.loaded_ids) < view.named_total
        view.overview_initialized = True
        view.status = "ready"
        view.message = None
        view.refreshed_at = attempted_at
        view.next_refresh_at = self._clock() + (self.scan_seconds if view.has_more else self.refresh_seconds)

    async def _refresh_completed_overview(self, view: _IndexView, attempted_at: str) -> None:
        """Re-read a completed overview in bounded pages before atomically swapping it.

        The formerly completed public frame remains visible and retains its old
        timestamp until the replacement coverage and every selected named page
        have actually completed.  An in-progress scan is therefore never made
        artificially fresh merely by a heartbeat.
        """
        if not view.overview_refresh_started:
            clusters, total = await self._read_clusters(view)
            named_total = await asyncio.to_thread(
                self.repository.overview_count,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
                min_population=view.overview_min_population,
            )
            view.overview_refresh_started = True
            view.overview_refresh_clusters = clusters
            view.overview_refresh_total = total
            view.overview_refresh_named_total = named_total
            view.overview_refresh_ids = {}
            view.overview_refresh_cursor = 0

        if len(view.overview_refresh_ids) < view.overview_refresh_named_total:
            rows = await asyncio.to_thread(
                self.repository.overview_page,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
                min_population=view.overview_min_population,
                after_id=view.overview_refresh_cursor,
                limit=PLACE_PAGE_SIZE,
            )
            self._append_markers(
                view.overview_refresh_ids,
                rows,
                cursor_owner=view,
                refreshed_at=attempted_at,
                overview=True,
                refresh_cursor=True,
            )

        if len(view.overview_refresh_ids) == view.overview_refresh_named_total:
            view.coverage_clusters = view.overview_refresh_clusters
            view.total_in_view = view.overview_refresh_total
            view.named_total = view.overview_refresh_named_total
            view.loaded_ids = view.overview_refresh_ids
            view.cursor = view.overview_refresh_cursor
            view.places = self._overview_places(view)
            view.has_more = False
            view.refreshed_at = attempted_at
            view.overview_refresh_started = False
            view.overview_refresh_clusters = []
            view.overview_refresh_total = 0
            view.overview_refresh_named_total = 0
            view.overview_refresh_ids = {}
            view.overview_refresh_cursor = 0
            view.status = "ready"
            view.message = None
            view.next_refresh_at = self._clock() + self.refresh_seconds
        else:
            # Preserve the old complete frame/timestamp during the next page.
            view.next_refresh_at = self._clock() + self.scan_seconds

    async def _read_clusters(self, view: _IndexView) -> tuple[list[dict[str, object]], int]:
        cell_size = self._cluster_cell_size(view.bounds)
        rows = await asyncio.to_thread(
            self.repository.cluster_rows,
            south=view.bounds.south,
            north=view.bounds.north,
            west=view.bounds.west,
            east=view.bounds.east,
            cell_size=cell_size,
        )
        places = [self._cluster_marker(row, cell_size=cell_size, zoom=view.bounds.zoom) for row in rows]
        places.sort(key=lambda item: str(item["id"]))
        total = sum(
            int(cluster.get("count", 0))
            for item in places
            if isinstance(cluster := item.get("cluster"), dict)
        )
        return places, total

    async def _refresh_places_view(self, view: _IndexView, attempted_at: str) -> None:
        if view.loaded_ids and not view.has_more:
            await self._refresh_completed_places(view, attempted_at)
            return
        if view.total_in_view == 0 and view.cursor == 0 and not view.loaded_ids:
            view.total_in_view = await asyncio.to_thread(
                self.repository.count_places,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
            )
        rows: list[tuple] = []
        if len(view.loaded_ids) < view.total_in_view:
            rows = await asyncio.to_thread(
                self.repository.place_page,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
                after_id=view.cursor,
                limit=PLACE_PAGE_SIZE,
            )
        self._append_markers(view.loaded_ids, rows, cursor_owner=view, refreshed_at=attempted_at, overview=False)
        view.places = [view.loaded_ids[identifier] for identifier in sorted(view.loaded_ids)]
        view.has_more = len(view.loaded_ids) < view.total_in_view
        view.status = "ready"
        view.message = None
        view.refreshed_at = attempted_at
        view.next_refresh_at = self._clock() + (self.scan_seconds if view.has_more else self.refresh_seconds)

    async def _refresh_completed_places(self, view: _IndexView, attempted_at: str) -> None:
        # A full view is reread in bounded pages, not merely given a new
        # timestamp. Keep its last complete snapshot visible while a fresh scan
        # is assembled; coordinate/label updates publish atomically.
        rows = await asyncio.to_thread(
            self.repository.place_page,
            south=view.bounds.south,
            north=view.bounds.north,
            west=view.bounds.west,
            east=view.bounds.east,
            after_id=view.refresh_cursor,
            limit=PLACE_PAGE_SIZE,
        )
        next_ids = dict(view.refresh_ids)
        next_cursor = view.refresh_cursor
        for row in rows:
            marker = self._place_marker(row, refreshed_at=attempted_at, overview=False)
            identifier = int(str(marker["id"]).split(":", 1)[1])
            next_ids[identifier] = marker
            next_cursor = max(next_cursor, identifier)
        if len(rows) < PLACE_PAGE_SIZE:
            total = await asyncio.to_thread(
                self.repository.count_places,
                south=view.bounds.south,
                north=view.bounds.north,
                west=view.bounds.west,
                east=view.bounds.east,
            )
            # Inserts during a scan can occur below its keyset cursor. Retry
            # rather than silently claiming the missing IDs were loaded.
            if len(next_ids) != total:
                view.refresh_ids = {}
                view.refresh_cursor = 0
                view.next_refresh_at = self._clock() + self.scan_seconds
                return
            view.loaded_ids = next_ids
            view.cursor = next_cursor
            view.places = [next_ids[identifier] for identifier in sorted(next_ids)]
            view.total_in_view = total
            view.has_more = False
            view.refreshed_at = attempted_at
            view.refresh_ids = {}
            view.refresh_cursor = 0
            view.status = "ready"
            view.message = None
        else:
            view.refresh_ids = next_ids
            view.refresh_cursor = next_cursor
        # Do not overwrite ``refreshed_at`` while a completed frame is being
        # rescanned; a progress tick is not a fresh public snapshot.
        view.next_refresh_at = self._clock() + (self.scan_seconds if len(rows) >= PLACE_PAGE_SIZE else self.refresh_seconds)

    def _append_markers(
        self,
        destination: dict[int, dict[str, object]],
        rows: list[tuple],
        *,
        cursor_owner: _IndexView,
        refreshed_at: str,
        overview: bool,
        refresh_cursor: bool = False,
    ) -> None:
        cursor = cursor_owner.overview_refresh_cursor if refresh_cursor else cursor_owner.cursor
        for row in rows:
            marker = self._place_marker(row, refreshed_at=refreshed_at, overview=overview)
            geoname_id = int(str(marker["id"]).split(":", 1)[1])
            destination[geoname_id] = marker
            cursor = max(cursor, geoname_id)
        if refresh_cursor:
            cursor_owner.overview_refresh_cursor = cursor
        else:
            cursor_owner.cursor = cursor

    def _overview_places(self, view: _IndexView) -> list[dict[str, object]]:
        named = [view.loaded_ids[identifier] for identifier in sorted(view.loaded_ids)]
        return named + list(view.coverage_clusters)

    def _public_view(self, view: _IndexView) -> dict[str, object]:
        running = self.worker_running
        overview = view.bounds.zoom < 10
        status = view.status
        message = view.message
        refreshed_at = view.refreshed_at
        has_more = bool(view.has_more)
        total_in_view = int(view.total_in_view)
        loaded_count = len(view.loaded_ids) if not overview else min(len(view.loaded_ids), view.named_total)
        places = view.places
        # A street box must not sit on "loading" while the database page is
        # still queued. Publish a complete empty city frame immediately; the
        # background read fills real cities into the next frame.
        if (
            running
            and not overview
            and view.bounds.zoom >= 12
            and status == "loading"
            and not places
        ):
            status = "ready"
            message = None
            refreshed_at = utc_now()
            has_more = False
            total_in_view = 0
            loaded_count = 0
        if not running:
            status = "error"
            message = self._terminal_error or "index worker is not running"
        result: dict[str, object] = {
            "status": status,
            "places": copy.deepcopy(places),
            "dataset": copy.deepcopy(self._dataset),
            "worker_running": running,
            "refreshed_at": refreshed_at,
            "total_in_view": total_in_view,
            "loaded_count": loaded_count,
            "has_more": has_more,
            "mode": "overview" if overview else "places",
        }
        if overview:
            result["coverage"] = {
                "represented_records": int(sum(
                    int(cluster.get("count", 0))
                    for item in view.coverage_clusters
                    if isinstance(cluster := item.get("cluster"), dict)
                )),
                "named_total": int(view.named_total),
                "named_loaded": min(len(view.loaded_ids), view.named_total),
                "cluster_count": len(view.coverage_clusters),
                "min_population": int(view.overview_min_population),
                "includes_capitals": True,
            }
        if message:
            result["message"] = message
        return result

    @staticmethod
    def validate_bounds(*, south: object, north: object, west: object, east: object, zoom: object) -> IndexBounds:
        values = (south, north, west, east)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
            raise HTTPException(status_code=422, detail="index bounds must be finite geographic coordinates")
        south_value, north_value, west_value, east_value = (float(cast(float, value)) for value in values)
        if not -90 <= south_value < north_value <= 90 or not -180 <= west_value <= 180 or not -180 <= east_value <= 180:
            raise HTTPException(status_code=422, detail="index bounds are outside valid geographic limits")
        if west_value == east_value:
            raise HTTPException(status_code=422, detail="index bounds must have nonzero longitude span")
        if isinstance(zoom, bool) or not isinstance(zoom, int) or not 2 <= zoom <= 20:
            raise HTTPException(status_code=422, detail="index requires integer zoom from 2 through 20")
        return IndexBounds(
            south=round(south_value, 4), north=round(north_value, 4),
            west=round(west_value, 4), east=round(east_value, 4), zoom=zoom,
        )

    @staticmethod
    def _view_key(bounds: IndexBounds) -> str:
        return f"{bounds.south:.4f}:{bounds.north:.4f}:{bounds.west:.4f}:{bounds.east:.4f}:{bounds.zoom}"

    @staticmethod
    def _longitude_span(bounds: IndexBounds) -> float:
        return bounds.east - bounds.west if bounds.west <= bounds.east else 360.0 - bounds.west + bounds.east

    @classmethod
    def _cluster_cell_size(cls, bounds: IndexBounds) -> float:
        lat_span = bounds.north - bounds.south
        lng_span = cls._longitude_span(bounds)
        # At z2 the native grid is 11.25 degrees (360 / 2**(2 + 3)), rather
        # than the prior 90-degree eight-cluster world. The adaptive 20-by-20
        # maximum keeps a viewport below roughly 441 boundary-aligned cells
        # without truncating any geographic records.
        zoom_cell = 360.0 / (2 ** (bounds.zoom + 3))
        adaptive_cell = max(
            lat_span / 20.0,
            lng_span / 20.0,
            math.sqrt((lat_span * lng_span) / 320.0),
        )
        return max(0.0001, round(max(zoom_cell, adaptive_cell), 6))

    @staticmethod
    def _overview_min_population(zoom: int) -> int:
        return OVERVIEW_POPULATION_BY_ZOOM[zoom]

    def _cluster_marker(self, row: tuple, *, cell_size: float, zoom: int) -> dict[str, object]:
        lat_cell, lng_cell, count, mean_lat, mean_lng = row
        lat_index = int(lat_cell)
        lng_index = int(lng_cell)
        size_key = f"{cell_size:.6f}"
        version = str(self._dataset.get("version", "unimported"))
        cluster_id = f"gncluster:{version}:z{zoom}:g{size_key}:{lat_index}:{lng_index}"
        south = max(-90.0, -90.0 + lat_index * cell_size)
        north = min(90.0, -90.0 + (lat_index + 1) * cell_size)
        west = max(-180.0, -180.0 + lng_index * cell_size)
        east = min(180.0, -180.0 + (lng_index + 1) * cell_size)
        numeric_count = int(count)
        name = f"{numeric_count} indexed places"
        return {
            "id": cluster_id,
            "name": name,
            "origin": "index-cluster",
            "lat": float(mean_lat),
            "lng": float(mean_lng),
            "cluster": {
                "count": numeric_count,
                "bounds": {"south": south, "north": north, "west": west, "east": east},
            },
            "marker": {
                "title": name,
                "label": name,
                "color": "#467da6",
                "popup": {"heading": name, "provenance": INDEX_PROVENANCE},
            },
            "refreshed_at": utc_now(),
        }

    @staticmethod
    def _safe_text(value: object, *, fallback: str, maximum: int = 255) -> str:
        if not isinstance(value, str):
            return fallback
        cleaned = " ".join(value.split())[:maximum]
        return cleaned or fallback

    def _place_marker(self, row: tuple, *, refreshed_at: str, overview: bool) -> dict[str, object]:
        geoname_id, name, latitude, longitude, country, feature_code, population, modification_date = row
        identifier = int(geoname_id)
        clean_name = self._safe_text(name, fallback="Indexed place")
        supplied_country = self._safe_text(country, fallback="ZZ", maximum=8).upper()
        clean_country = supplied_country if _COUNTRY_CODE.fullmatch(supplied_country) else "ZZ"
        clean_feature = self._safe_text(feature_code, fallback="unknown", maximum=32)
        try:
            numeric_population = max(0, int(population))
        except (TypeError, ValueError, OverflowError):
            numeric_population = 0
        return {
            "id": f"geonames:{identifier}",
            "name": clean_name,
            "origin": "open-index",
            "lat": float(latitude),
            "lng": float(longitude),
            "address": clean_country,
            "category": clean_feature,
            "country_code": clean_country,
            "population": numeric_population,
            "overview": overview,
            "data_modified_at": str(modification_date),
            "marker": {
                "title": clean_name,
                "label": clean_name,
                "color": "#467da6",
                "popup": {"heading": clean_name, "provenance": INDEX_PROVENANCE},
            },
            "refreshed_at": refreshed_at,
        }

    def _expire(self, now: float) -> None:
        expired = [key for key, view in self._views.items() if now - view.last_seen > self.expiry_seconds]
        for key in expired:
            self._views.pop(key, None)
