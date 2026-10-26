"""Application-owned, non-networked marker coordinate refresh runtime.

The immutable source is only executed by :mod:`server.source_gate` during
application startup.  This module reads literal coordinates with the existing
AST extractor; it never imports or re-executes the source artifact.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .source_gate import EXPECTED_SHA256, SOURCE_PATH
from .source_locations import SourceLocationError, extract_approved_locations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_UPDATES_PATH = PROJECT_ROOT / "runtime" / "marker_coordinates.json"
DEFAULT_INTERVAL_SECONDS = 5.0
MAX_UPDATE_BYTES = 128 * 1024
MAX_UPDATE_RECORDS = 1000
LOCAL_UPDATE_PROVENANCE = "Server coordinate update — not hardware-verified telemetry"


class ImmutableSourceError(RuntimeError):
    """The previously approved source can no longer be safely indexed."""


class UpdateDocumentError(ValueError):
    """The operator-owned local update document is malformed."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    """Hash a local file in bounded chunks without exposing its content."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source_file:
            for block in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ImmutableSourceError("approved source verification failed") from exc
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateDocumentError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise UpdateDocumentError("non-finite JSON value")


def _normalise_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise UpdateDocumentError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise UpdateDocumentError("timestamp must include an offset")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise UpdateDocumentError("invalid timestamp") from exc


def _coordinate(value: object, *, latitude: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpdateDocumentError("invalid coordinate")
    try:
        coordinate = float(value)
    except (OverflowError, ValueError) as exc:
        raise UpdateDocumentError("invalid coordinate") from exc
    maximum = 90.0 if latitude else 180.0
    if not math.isfinite(coordinate) or not -maximum <= coordinate <= maximum:
        raise UpdateDocumentError("invalid coordinate")
    return coordinate


class MarkerRuntime:
    """Refresh a safe marker snapshot every few seconds while the app is alive.

    The optional update file is intentionally local-only; this class creates no
    HTTP write route, remote feed, socket, or provider connection.  Every
    refresh hashes the immutable source.  Its AST coordinate index is cached
    when that expected hash has not changed, so the large source is not parsed
    again on the normal five-second heartbeat.
    """

    def __init__(
        self,
        *,
        source_path: Path = SOURCE_PATH,
        expected_sha256: str = EXPECTED_SHA256,
        updates_path: Path = RUNTIME_UPDATES_PATH,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        nearby_runtime: Any | None = None,
        index_runtime: Any | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("marker interval must be positive")
        self.source_path = Path(source_path)
        self.expected_sha256 = expected_sha256
        self.updates_path = Path(updates_path)
        self.interval_seconds = float(interval_seconds)

        self._cached_source_hash: str | None = None
        self._base_records: list[dict[str, object]] | None = None
        self._snapshot: list[dict[str, object]] | None = None
        self._coordinates_mode = "source-static"
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # The source-extension child uses ``initialize`` and explicitly calls
        # ``refresh_once`` from its extension-owned loop. Existing app/library
        # callers retain ``start`` and its owned compatibility task.
        self._externally_driven = False
        self._fatal = False
        # The nearby worker is optional so existing standalone marker-runtime
        # users/tests still run with no Maps dependency.  When supplied, its
        # task is nevertheless owned entirely by this runtime's lifecycle.
        self._nearby_runtime = nearby_runtime
        # The own-database geographic index shares the source-gated lifecycle
        # but remains independent of provider-backed nearby results.
        self._index_runtime = index_runtime

        self.status = "stopped"
        self.error: str | None = None
        self.cycle_count = 0
        self.revision = 0
        self.changed_count = 0
        self.last_refresh_at: str | None = None
        self.last_changed_at: str | None = None

    @property
    def fatal(self) -> bool:
        return self._fatal

    @property
    def ready(self) -> bool:
        """A transient update-file error is healthy while this task is alive."""
        return not self._fatal and self._running and (
            self._externally_driven or (self._task is not None and not self._task.done())
        )

    @property
    def nearby_ready(self) -> bool:
        """Do not claim full worker health after an attached child task dies."""
        if self._nearby_runtime is None:
            return True
        return bool(getattr(self._nearby_runtime, "ready", False))

    @property
    def index_ready(self) -> bool:
        """The child task may report an unimported dataset without being dead."""
        if self._index_runtime is None:
            return True
        return bool(getattr(self._index_runtime, "ready", False))

    def attach_nearby(self, nearby_runtime: Any) -> None:
        """Bind a nearby child before start without adding a Maps requirement."""
        if self._task is not None or self._running:
            raise RuntimeError("nearby runtime must be attached before marker runtime starts")
        self._nearby_runtime = nearby_runtime

    def attach_index(self, index_runtime: Any) -> None:
        """Bind the own-index child before marker runtime startup."""
        if self._task is not None or self._running:
            raise RuntimeError("index runtime must be attached before marker runtime starts")
        self._index_runtime = index_runtime

    async def start(self) -> None:
        """Perform the mandatory first refresh before scheduling heartbeats."""
        if self._task is not None or self._running:
            return
        self._running = True
        self._externally_driven = False
        self.status = "running"
        await self.refresh_once()
        if self._fatal:
            self._running = False
            return
        self._task = asyncio.create_task(self._run(), name="cherrymap-marker-worker")
        if self._nearby_runtime is not None or self._index_runtime is not None:
            try:
                # This happens only after the initial source hash/AST snapshot
                # is safe.  NearbyRuntime.start itself performs no provider I/O.
                if self._nearby_runtime is not None:
                    await self._nearby_runtime.start()
                if self._index_runtime is not None:
                    # Importing is explicit-only; this starts only the bounded
                    # metadata and registered-viewport query child.
                    await self._index_runtime.start()
            except Exception:
                self._fatal = True
                self._running = False
                self.status = "error"
                self.error = "marker worker stopped unexpectedly"
                await self._stop_children(source_fatal=True)
                task = self._task
                self._task = None
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def initialize(self) -> None:
        """Perform startup work without creating a helper-owned refresh task.

        The dedicated source-extension child calls this exactly once, then
        owns scheduling by awaiting ``refresh_once`` in its extension loop.
        """
        if self._task is not None:
            raise RuntimeError("marker runtime already owns a refresh task")
        if self._running:
            return
        self._running = True
        self._externally_driven = True
        self.status = "running"
        await self.refresh_once()
        if self._fatal:
            self._running = False

    async def stop(self) -> None:
        """Cancel and await the app-owned task; no worker outlives the lifespan."""
        self._running = False
        self._externally_driven = False
        await self._stop_children(source_fatal=self._fatal)
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not self._fatal:
            self.status = "stopped"

    async def _stop_nearby(self, *, source_fatal: bool) -> None:
        """Stop the optional child promptly without letting it block source work."""
        if self._nearby_runtime is None:
            return
        try:
            await self._nearby_runtime.stop(source_fatal=source_fatal)
        except Exception:
            # A nearby cleanup failure cannot revive or hide source failure.
            pass

    async def _stop_index(self, *, source_fatal: bool) -> None:
        if self._index_runtime is None:
            return
        try:
            await self._index_runtime.stop(source_fatal=source_fatal)
        except Exception:
            # Index cleanup failures cannot hide a source failure.
            pass

    async def _stop_children(self, *, source_fatal: bool) -> None:
        await self._stop_nearby(source_fatal=source_fatal)
        await self._stop_index(source_fatal=source_fatal)

    async def _run(self) -> None:
        try:
            while self._running and not self._fatal:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                await self.refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep a fixed public error; never surface a local input/path detail.
            self._fatal = True
            self._running = False
            self.status = "error"
            self.error = "marker worker stopped unexpectedly"
            await self._stop_children(source_fatal=True)
        finally:
            if not self._fatal:
                self._running = False
                self.status = "stopped"

    async def refresh_once(self) -> bool:
        """Refresh one snapshot without blocking FastAPI's event loop.

        ``changed_count`` is the number of marker IDs whose complete public
        record changed in this cycle.  The initial source snapshot counts all
        published IDs as changed from an empty snapshot.  Invalid local input
        reports an error with a zero changed count and retains the last good
        public snapshot atomically.
        """
        self.cycle_count += 1
        attempted_at = _utc_now()
        try:
            candidate, coordinates_mode = await asyncio.to_thread(self._read_candidate)
        except UpdateDocumentError:
            # A malformed file present at first startup has no prior published
            # update snapshot.  The already verified AST source index is still
            # a safe last-good baseline; never publish an empty or invented map.
            self._publish_initial_source_baseline(attempted_at)
            self.status = "error"
            self.error = "local marker update document is invalid"
            self.changed_count = 0
            self.last_refresh_at = attempted_at
            return False
        except (ImmutableSourceError, SourceLocationError):
            self._fatal = True
            self._running = False
            self.status = "error"
            self.error = "approved source verification failed"
            self.changed_count = 0
            self.last_refresh_at = attempted_at
            await self._stop_children(source_fatal=True)
            return False
        except Exception:
            self._fatal = True
            self._running = False
            self.status = "error"
            self.error = "marker worker stopped unexpectedly"
            self.changed_count = 0
            self.last_refresh_at = attempted_at
            await self._stop_children(source_fatal=True)
            return False

        previous = self._snapshot
        changed_ids = self._changed_ids(previous, candidate)
        self.changed_count = len(changed_ids)
        self._snapshot = candidate
        self._coordinates_mode = coordinates_mode
        self.last_refresh_at = attempted_at
        if previous is None:
            self.revision = 1
            self.last_changed_at = attempted_at
        elif changed_ids:
            self.revision += 1
            self.last_changed_at = attempted_at
        self.status = "running"
        self.error = None
        return True

    def _publish_initial_source_baseline(self, changed_at: str) -> None:
        if self._snapshot is not None or self._base_records is None:
            return
        self._snapshot = copy.deepcopy(self._base_records)
        self._coordinates_mode = "source-static"
        self.revision = 1
        self.last_changed_at = changed_at

    def locations(self) -> list[dict[str, object]]:
        """Return an isolated copy so routes cannot mutate the cached snapshot."""
        return copy.deepcopy(self._snapshot or [])

    def nearby_snapshot(
        self,
        *,
        enabled: bool = False,
        lat: float | None = None,
        lng: float | None = None,
        radius: int | None = None,
    ) -> dict[str, object]:
        """Read/register an optional nearby view without coupling source markers.

        A disabled request must not mutate, stop, or otherwise affect another
        browser's active nearby view.  The attached child owns its bounded
        cache; standalone MarkerRuntime instances retain a stable error shape
        if a caller explicitly asks for nearby data without that dependency.
        """
        if not enabled:
            return {
                "status": "hidden",
                "places": [],
                "has_more": False,
                "last_refresh_at": None,
                "refresh_seconds": 60,
                "worker_running": bool(
                    self._nearby_runtime is not None and getattr(self._nearby_runtime, "worker_running", False)
                ),
                "coverage": {"radius_meters": None, "result_limit": 20, "exhaustive": False},
            }
        if self._nearby_runtime is None:
            return {
                "status": "error",
                "places": [],
                "has_more": False,
                "last_refresh_at": None,
                "refresh_seconds": 60,
                "worker_running": False,
                "coverage": {"radius_meters": radius, "result_limit": 20, "exhaustive": False},
                "message": "nearby worker is unavailable",
            }
        if lat is None or lng is None or radius is None:
            # Route validation normally prevents this.  Preserve a fixed
            # non-provider error rather than constructing an arbitrary view.
            return {
                "status": "error",
                "places": [],
                "has_more": False,
                "last_refresh_at": None,
                "refresh_seconds": 60,
                "worker_running": bool(getattr(self._nearby_runtime, "worker_running", False)),
                "coverage": {"radius_meters": radius, "result_limit": 20, "exhaustive": False},
                "message": "nearby view is invalid",
            }
        return self._nearby_runtime.snapshot_for_view(lat=lat, lng=lng, radius=radius)

    def index_snapshot(
        self,
        *,
        enabled: bool = False,
        south: float | None = None,
        north: float | None = None,
        west: float | None = None,
        east: float | None = None,
        zoom: int | None = None,
    ) -> dict[str, object]:
        """Read/register a bounded own-index view without request-time SQL."""
        if self._index_runtime is None:
            return {
                "status": "error" if enabled else "hidden",
                "places": [],
                "dataset": {
                    "name": "GeoNames cities500", "record_count": 0, "imported_at": None,
                    "source_url": "https://download.geonames.org/export/dump/cities500.zip",
                    "license": "CC BY 4.0", "version": "unimported",
                },
                "worker_running": False,
                "refreshed_at": None,
                "total_in_view": 0,
                "loaded_count": 0,
                "has_more": False,
                "mode": "clusters",
                **({"message": "index worker is unavailable"} if enabled else {}),
            }
        if not enabled:
            return self._index_runtime.snapshot()
        if None in (south, north, west, east, zoom):
            return {
                **self._index_runtime.snapshot(),
                "status": "error",
                "message": "index view is invalid",
            }
        return self._index_runtime.snapshot_for_view(
            south=float(south), north=float(north), west=float(west), east=float(east), zoom=int(zoom)
        )

    def public_worker(self) -> dict[str, object]:
        return {
            "running": self._running and not self._fatal,
            "status": self.status,
            "interval_seconds": self.interval_seconds,
            "cycle_count": self.cycle_count,
            "revision": self.revision,
            "last_refresh_at": self.last_refresh_at,
            "last_changed_at": self.last_changed_at,
            "coordinates_mode": self._coordinates_mode,
            "error": self.error,
            "changed_count": self.changed_count,
        }

    def _read_candidate(self) -> tuple[list[dict[str, object]], str]:
        base_records = self._load_base_records()
        updates = self._read_updates({str(record["id"]) for record in base_records})
        return self._apply_updates(base_records, updates)

    def _load_base_records(self) -> list[dict[str, object]]:
        actual_hash = _sha256(self.source_path)
        if actual_hash != self.expected_sha256:
            raise ImmutableSourceError("approved source verification failed")
        if self._base_records is None or self._cached_source_hash != actual_hash:
            try:
                locations = extract_approved_locations(self.source_path)
            except SourceLocationError:
                raise
            # Protect against a source swap between hash and AST read.
            if _sha256(self.source_path) != actual_hash:
                raise ImmutableSourceError("approved source verification failed")
            self._base_records = [
                {
                    **location.public_dict(),
                    "position_origin": "source-static",
                    "observed_at": None,
                }
                for location in locations
            ]
            self._cached_source_hash = actual_hash
        return copy.deepcopy(self._base_records)

    def _read_updates(self, known_ids: set[str]) -> dict[str, dict[str, object]]:
        try:
            with self.updates_path.open("rb") as update_file:
                raw = update_file.read(MAX_UPDATE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise UpdateDocumentError("unreadable update document") from exc
        if len(raw) > MAX_UPDATE_BYTES:
            raise UpdateDocumentError("update document too large")
        if not raw.strip():
            return {}
        try:
            decoded = raw.decode("utf-8")
            document = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise UpdateDocumentError("invalid update document") from exc
        if not isinstance(document, dict) or set(document) != {"updates"}:
            raise UpdateDocumentError("invalid update document shape")
        items = document["updates"]
        if not isinstance(items, list) or len(items) > MAX_UPDATE_RECORDS:
            raise UpdateDocumentError("invalid update document shape")

        updates: dict[str, dict[str, object]] = {}
        for item in items:
            if not isinstance(item, dict) or set(item) - {"id", "lat", "lng", "observed_at"}:
                raise UpdateDocumentError("invalid update record")
            if not {"id", "lat", "lng"}.issubset(item):
                raise UpdateDocumentError("invalid update record")
            identifier = item["id"]
            if not isinstance(identifier, str) or identifier not in known_ids or identifier in updates:
                raise UpdateDocumentError("invalid update record")
            observed_at = None
            if "observed_at" in item:
                observed_at = _normalise_timestamp(item["observed_at"])
            updates[identifier] = {
                "lat": _coordinate(item["lat"], latitude=True),
                "lng": _coordinate(item["lng"], latitude=False),
                "observed_at": observed_at,
            }
        return updates

    @staticmethod
    def _apply_updates(
        base_records: list[dict[str, object]], updates: dict[str, dict[str, object]],
    ) -> tuple[list[dict[str, object]], str]:
        if not updates:
            return base_records, "source-static"
        records: list[dict[str, object]] = []
        for base in base_records:
            record = copy.deepcopy(base)
            update = updates.get(str(record["id"]))
            if update is not None:
                record["lat"] = update["lat"]
                record["lng"] = update["lng"]
                record["position_origin"] = "local-update"
                record["observed_at"] = update["observed_at"]
                marker = record["marker"]
                if isinstance(marker, dict):
                    popup = marker.get("popup")
                    if isinstance(popup, dict):
                        popup["provenance"] = LOCAL_UPDATE_PROVENANCE
            records.append(record)
        return records, "local-updates"

    @staticmethod
    def _changed_ids(
        previous: list[dict[str, object]] | None,
        candidate: list[dict[str, object]],
    ) -> set[str]:
        if previous is None:
            return {str(record["id"]) for record in candidate}
        previous_by_id = {str(record["id"]): record for record in previous}
        candidate_by_id = {str(record["id"]): record for record in candidate}
        return {
            identifier
            for identifier in previous_by_id.keys() | candidate_by_id.keys()
            if previous_by_id.get(identifier) != candidate_by_id.get(identifier)
        }
