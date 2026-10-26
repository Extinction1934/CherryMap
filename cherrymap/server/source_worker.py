"""Supervise the immutable-source mapping extension child.

FastAPI only runs the mandatory :mod:`server.source_gate` first, then starts one
fixed launcher.  That launcher verifies and executes the *unchanged* original
``CherryPro_Z4_portable.py`` as ``__main__`` in its own PID before attaching the
separate mapping extension in that same PID.  Only bounded public JSON snapshots
cross this process boundary.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, TypeGuard

from .source_gate import EXPECTED_SHA256, SOURCE_PATH, SourceGateResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SOURCE = SOURCE_PATH
LAUNCHER_PATH = PROJECT_ROOT / "server" / "source_extension_runner.py"
EXTENSION_PATH = PROJECT_ROOT / "server" / "mapping_extension.py"
LAUNCHER_SHA_FILE = LAUNCHER_PATH.with_suffix(".py.sha256")
EXTENSION_SHA_FILE = EXTENSION_PATH.with_suffix(".py.sha256")
MAX_COMMAND_BYTES = 16_384
# A 500-record index page can appear both as ``index.places`` and fusion markers.
# The subprocess stream limit must therefore be well above the old 64 KiB default.
MAX_REPLY_BYTES = 8 * 1024 * 1024
START_TIMEOUT_SECONDS = 120.0
IPC_TIMEOUT_SECONDS = 8.0
HEARTBEAT_MAX_AGE_SECONDS = 20.0
_PLACE_ID = re.compile(r"[A-Za-z0-9_-]{1,180}\Z")


class SourceWorkerError(RuntimeError):
    """The supervised source-extension child cannot safely serve a snapshot."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _recorded_sha256(path: Path, record: Path) -> str:
    """Verify a deployment hash record; no code is downloaded or rendered."""
    if not path.is_file() or not record.is_file():
        raise SourceWorkerError("source extension artifact is unavailable")
    try:
        expected = record.read_text(encoding="ascii").strip().split()[0]
    except (OSError, UnicodeDecodeError, IndexError) as exc:
        raise SourceWorkerError("source extension fingerprint record is unavailable") from exc
    actual = _sha256(path)
    if len(expected) != 64 or expected != actual:
        raise SourceWorkerError("source extension fingerprint mismatch")
    return actual


def verify_extension_artifacts() -> tuple[str, str]:
    """Verify fixed launcher/extension files and the immutable original hash."""
    try:
        original_hash = _sha256(ORIGINAL_SOURCE)
    except OSError as exc:
        raise SourceWorkerError("approved source artifact is unavailable") from exc
    if original_hash != EXPECTED_SHA256:
        raise SourceWorkerError("approved source fingerprint mismatch")
    launcher_hash = _recorded_sha256(LAUNCHER_PATH, LAUNCHER_SHA_FILE)
    extension_hash = _recorded_sha256(EXTENSION_PATH, EXTENSION_SHA_FILE)
    return launcher_hash, extension_hash


def _child_environment(scratch: Path) -> dict[str, str]:
    """A minimal environment deliberately excludes DB, Maps, and Manus secrets."""
    return {
        "PATH": os.defpath,
        "HOME": str(scratch),
        "LANG": "C.UTF-8",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _private_values() -> dict[str, str]:
    """The only runtime configuration sent after original source execution."""
    return {
        "database_url": os.getenv("DATABASE_URL", ""),
        "maps_api_url": os.getenv("MANUS_API_URL", ""),
        "maps_api_key": os.getenv("MANUS_API_KEY", ""),
    }


def _contains_private(value: object, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, list):
        return any(_contains_private(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(_contains_private(item, secrets) for item in value.values())
    return False


def _runner_is_valid(runner: object, *, pid: int | None, extension_sha256: str | None) -> TypeGuard[dict[str, Any]]:
    if not isinstance(runner, dict):
        return False
    required = {
        "kind", "filename", "extension_filename", "pid", "started_at", "sha256",
        "original_sha256", "extension_sha256",
    }
    if set(runner) != required:
        return False
    return (
        runner.get("kind") == "cherrypro-source-extension"
        and runner.get("filename") == ORIGINAL_SOURCE.name
        and runner.get("extension_filename") == EXTENSION_PATH.name
        and isinstance(runner.get("pid"), int)
        and (pid is None or runner.get("pid") == pid)
        and isinstance(runner.get("started_at"), str)
        and len(runner["started_at"]) <= 64
        and runner.get("sha256") == EXPECTED_SHA256
        and runner.get("original_sha256") == EXPECTED_SHA256
        and isinstance(runner.get("extension_sha256"), str)
        and (extension_sha256 is None or runner.get("extension_sha256") == extension_sha256)
    )


def _safe_public_record(record: object) -> bool:
    """Require an exact origin-specific safe shape, including nested popup data."""
    if not isinstance(record, dict) or len(record) > 16:
        return False
    identifier = record.get("id")
    if not isinstance(identifier, str):
        return False
    family = {"source": "source", "open-index": "index", "index-cluster": "index", "provider-worker": "provider", "open-poi": "poi", "neural-feed": "poi"}.get(str(record.get("origin")))
    if family is None:
        return False
    # Shared pure construction is also used in the child. This imports trusted
    # definitions only; it neither executes the uploaded source nor starts a loop.
    from .mapping_extension import _safe_record
    return _safe_record(record, family=family, known_source_ids={identifier}) == record


def _snapshot_is_public(snapshot: object, *, secrets: tuple[str, ...], pid: int | None, extension_sha256: str | None) -> TypeGuard[dict[str, Any]]:
    """Reject malformed, private, or unbounded frames before app state sees them."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"locations", "worker", "nearby", "index", "poi", "fusion"}:
        return False
    locations, worker, nearby, index, poi, fusion = (
        snapshot.get("locations"), snapshot.get("worker"), snapshot.get("nearby"), snapshot.get("index"), snapshot.get("poi"), snapshot.get("fusion")
    )
    if not isinstance(locations, list) or not isinstance(worker, dict) or not isinstance(nearby, dict) or not isinstance(index, dict) or not isinstance(poi, dict) or not isinstance(fusion, dict):
        return False
    runner = worker.get("runner")
    if not _runner_is_valid(runner, pid=pid, extension_sha256=extension_sha256):
        return False
    if not all(_safe_public_record(item) for item in locations):
        return False
    for collection in (nearby.get("places"), index.get("places"), poi.get("places"), fusion.get("markers")):
        if not isinstance(collection, list) or not all(_safe_public_record(item) for item in collection):
            return False
    counts = fusion.get("counts")
    if not isinstance(counts, dict) or set(counts) not in ({"source", "index", "provider", "total"}, {"source", "index", "provider", "poi", "total"}):
        return False
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        return False
    collections = (locations, index["places"], nearby["places"], poi["places"])
    origin_sets = ({"source"}, {"open-index", "index-cluster"}, {"provider-worker"}, {"open-poi", "neural-feed"})
    expected: dict[str, dict[str, Any]] = {}
    for collection, origins in zip(collections, origin_sets):
        if any(record["origin"] not in origins for record in collection): return False
        for record in collection: expected.setdefault(record["id"], record)
    expected_counts = {"source": len(locations), "index": len(index["places"]), "provider": len(nearby["places"]), "total": len(expected)}
    if "poi" in counts: expected_counts["poi"] = len(poi["places"])
    elif poi["places"]: return False
    markers = fusion["markers"]
    if counts != expected_counts or len(markers) != len(expected) or {record["id"]: record for record in markers} != expected:
        return False
    if fusion.get("policy") != "source-authoritative; preserve distinct index/provider provenance":
        return False
    if not isinstance(fusion.get("data_mode"), str) or "sensor" not in fusion["data_mode"].casefold():
        return False
    try:
        rendered = json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return len(rendered.encode("utf-8")) <= MAX_REPLY_BYTES and not _contains_private(snapshot, secrets)


class SourceWorkerSupervisor:
    """One non-restarting source-extension child owned by the FastAPI lifespan."""

    def __init__(
        self,
        *,
        launcher_path: Path = LAUNCHER_PATH,
        start_timeout: float = START_TIMEOUT_SECONDS,
        ipc_timeout: float = IPC_TIMEOUT_SECONDS,
    ) -> None:
        self.launcher_path = Path(launcher_path)
        self.start_timeout = float(start_timeout)
        self.ipc_timeout = float(ipc_timeout)
        self.process: asyncio.subprocess.Process | None = None
        self.child_argv: tuple[str, ...] = ()
        self._scratch: Path | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._pending_places: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._pending_searches: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._write_lock = asyncio.Lock()
        self._bootstrap = asyncio.Event()
        self._ready = asyncio.Event()
        self._stopping = False
        self._fatal_error: str | None = None
        self._snapshot: dict[str, object] | None = None
        self._runner: dict[str, object] | None = None
        self._last_heartbeat = 0.0
        self._launcher_sha256: str | None = None
        self._extension_sha256: str | None = None
        self._private: dict[str, str] = {}

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None and self.process.returncode is None else None

    @property
    def last_heartbeat_age(self) -> float:
        return float("inf") if self._last_heartbeat <= 0 else max(0.0, time.monotonic() - self._last_heartbeat)

    @property
    def ready(self) -> bool:
        return bool(self._ready.is_set() and self.pid is not None and self._fatal_error is None and self.last_heartbeat_age <= HEARTBEAT_MAX_AGE_SECONDS)

    @property
    def error(self) -> str | None:
        return self._fatal_error

    def public_worker(self) -> dict[str, object]:
        cached_worker = self._snapshot.get("worker") if self._snapshot is not None else None
        if isinstance(cached_worker, dict):
            result = copy.deepcopy(cached_worker)
        else:
            result = {
                "running": False, "status": "error" if self._fatal_error else "stopped", "interval_seconds": 5.0,
                "cycle_count": 0, "revision": 0, "last_refresh_at": None, "last_changed_at": None,
                "coordinates_mode": "source-static", "error": self._fatal_error, "changed_count": 0,
                "sensor_data": "not configured", "tpm_status": "unverified",
            }
        if self._runner is not None:
            result["runner"] = copy.deepcopy(self._runner)
        result["running"] = bool(self.ready and result.get("running"))
        if not self.ready:
            result["status"] = "error" if self._fatal_error else "stopped"
            result["error"] = self._fatal_error or result.get("error")
        return result

    def _fail(self, category: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = category
        # Wake either startup wait immediately instead of consuming 45 seconds.
        self._bootstrap.set()
        self._ready.set()

    async def start(self, gate_result: SourceGateResult | Any) -> None:
        """Launch after the original mandatory gate, never before it."""
        if getattr(gate_result, "status", None) != "passed":
            raise SourceWorkerError("approved source startup gate has not completed")
        if self.process is not None:
            return
        if self.launcher_path.resolve() != LAUNCHER_PATH.resolve():
            raise SourceWorkerError("source extension launcher path is invalid")
        self._launcher_sha256, self._extension_sha256 = verify_extension_artifacts()
        self._private = _private_values()
        self._scratch = Path(tempfile.mkdtemp(prefix="cherrymap-source-extension-"))
        self._scratch.chmod(0o700)
        self.child_argv = (sys.executable, "-B", str(self.launcher_path.resolve()), "--source-extension")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.child_argv,
                cwd=str(self._scratch),
                env=_child_environment(self._scratch),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_REPLY_BYTES + 1,
            )
            self._reader_task = asyncio.create_task(self._read_stdout(), name="cherrymap-source-extension-stdout")
            await asyncio.wait_for(self._bootstrap.wait(), timeout=self.start_timeout)
            if self._fatal_error:
                raise SourceWorkerError(self._fatal_error)
            await self._send({
                "op": "init",
                "config": {
                    **self._private,
                    "original_source": str(ORIGINAL_SOURCE.resolve()),
                    "original_sha256": EXPECTED_SHA256,
                    "updates_path": str((PROJECT_ROOT / "runtime" / "marker_coordinates.json").resolve()),
                    "launcher_sha256": self._launcher_sha256,
                    "extension_sha256": self._extension_sha256,
                },
            })
            await asyncio.wait_for(self._ready.wait(), timeout=self.start_timeout)
            if not self.ready:
                raise SourceWorkerError(self._fatal_error or "source extension did not become healthy")
        except Exception as exc:
            await self.stop()
            if isinstance(exc, SourceWorkerError):
                raise
            raise SourceWorkerError("source extension startup failed") from exc

    async def _send(self, message: dict[str, object]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise SourceWorkerError("source extension is unavailable")
        try:
            encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise SourceWorkerError("source extension command is invalid") from exc
        if len(encoded) > MAX_COMMAND_BYTES:
            raise SourceWorkerError("source extension command is too large")
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self._fail("source extension emitted an oversized response")
                    break
                if not line:
                    break
                if len(line) > MAX_REPLY_BYTES:
                    self._fail("source extension emitted an oversized response")
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._fail("source extension emitted an invalid response")
                    break
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    self._fail("source extension emitted an invalid response")
                    break
                await self._accept_message(message)
                if self._fatal_error:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("source extension communication failed")
        finally:
            if not self._stopping and self._fatal_error is None:
                self._fail("source extension stopped unexpectedly")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(SourceWorkerError(self._fatal_error or "source extension is unavailable"))
            self._pending.clear()
            for future in self._pending_places.values():
                if not future.done():
                    future.set_exception(SourceWorkerError(self._fatal_error or "source extension is unavailable"))
            self._pending_places.clear()
            for future in self._pending_searches.values():
                if not future.done(): future.set_exception(SourceWorkerError(self._fatal_error or "source extension is unavailable"))
            self._pending_searches.clear()

    async def _accept_message(self, message: dict[str, object]) -> None:
        message_type = message["type"]
        if message_type == "bootstrap-complete":
            if set(message) != {"type", "runner", "body_executed"} or message.get("body_executed") is not True:
                self._fail("source extension bootstrap verification failed")
                return
            runner = message.get("runner")
            if not _runner_is_valid(runner, pid=self.pid, extension_sha256=self._extension_sha256):
                self._fail("source extension fingerprint mismatch")
                return
            self._runner = copy.deepcopy(runner)
            self._bootstrap.set()
            return
        if message_type in {"ready", "heartbeat"}:
            if set(message) != {"type", "snapshot"}:
                self._fail("source extension emitted an invalid response")
                return
            snapshot = message.get("snapshot")
            if not _snapshot_is_public(snapshot, secrets=tuple(self._private.values()), pid=self.pid, extension_sha256=self._extension_sha256):
                self._fail("source extension returned an unsafe snapshot")
                return
            self._snapshot = copy.deepcopy(snapshot)
            self._runner = copy.deepcopy(snapshot["worker"]["runner"])
            self._last_heartbeat = time.monotonic()
            if message_type == "ready":
                self._ready.set()
            return
        if message_type == "reply":
            if set(message) != {"type", "request_id", "snapshot"}:
                self._fail("source extension emitted an invalid response")
                return
            request_id = message.get("request_id")
            if not isinstance(request_id, str):
                self._fail("source extension emitted an invalid response")
                return
            future = self._pending.pop(request_id, None)
            if future is None:
                return
            snapshot = message.get("snapshot")
            if not _snapshot_is_public(snapshot, secrets=tuple(self._private.values()), pid=self.pid, extension_sha256=self._extension_sha256):
                self._fail("source extension returned an unsafe snapshot")
                future.set_exception(SourceWorkerError("source extension returned an unsafe snapshot"))
                return
            self._snapshot = copy.deepcopy(snapshot)
            self._runner = copy.deepcopy(snapshot["worker"]["runner"])
            self._last_heartbeat = time.monotonic()
            future.set_result(copy.deepcopy(snapshot))
            return
        if message_type == "place-reply":
            if set(message) != {"type", "request_id", "place", "fusion", "worker"} or not isinstance(message.get("request_id"), str):
                self._fail("source extension emitted an invalid response")
                return
            request_id = str(message["request_id"])
            future = self._pending_places.pop(request_id, None)
            if future is None:
                return
            place = message.get("place")
            if place is None:
                future.set_exception(SourceWorkerError("selected provider place is unavailable"))
                return
            if not _safe_public_record(place) or not isinstance(place, dict) or place.get("origin") != "provider-worker":
                self._fail("source extension returned an unsafe place")
                future.set_exception(SourceWorkerError("source extension returned an unsafe place"))
                return
            # Only the independently validated place crosses this endpoint.
            # Full fusion/worker metadata is served by the strict snapshot path.
            reply = {"place": copy.deepcopy(place)}
            if _contains_private(reply, tuple(self._private.values())):
                self._fail("source extension returned an unsafe place")
                future.set_exception(SourceWorkerError("source extension returned an unsafe place"))
                return
            future.set_result(reply)
            return
        if message_type == "search-reply":
            if set(message) != {"type", "request_id", "result"} or not isinstance(message.get("request_id"), str):
                self._fail("source extension emitted an invalid search response")
                return
            future = self._pending_searches.pop(str(message["request_id"]), None)
            if future is None: return
            result = message.get("result")
            if not isinstance(result, dict) or set(result) != {"results", "query", "scope", "count", "warnings"} or not isinstance(result.get("results"), list) or len(result["results"]) > 20:
                self._fail("source extension returned an unsafe search")
                future.set_exception(SourceWorkerError("source extension returned an unsafe search"))
                return
            if not isinstance(result.get("warnings"), list) or len(result["warnings"]) > 3 or any(not isinstance(message, str) or len(message) > 250 for message in result["warnings"]):
                self._fail("source extension returned an unsafe search")
                future.set_exception(SourceWorkerError("source extension returned an unsafe search"))
                return
            records = result["results"]
            if any(not _safe_public_record(record) for record in records) or result.get("count") != len(records) or result.get("scope") not in {"world", "nearby"} or not isinstance(result.get("query"), str) or not 1 <= len(result["query"]) <= 160 or _contains_private(result, tuple(self._private.values())):
                self._fail("source extension returned an unsafe search")
                future.set_exception(SourceWorkerError("source extension returned an unsafe search"))
                return
            future.set_result(copy.deepcopy(result))
            return
        if message_type == "fatal" and set(message) == {"type", "category"} and isinstance(message.get("category"), str):
            self._fail("source extension reported a safe failure")
            return
        self._fail("source extension emitted an invalid response")

    async def snapshot_for_query(self, query: dict[str, object]) -> dict[str, object]:
        """Ask the same-PID extension for a bounded current cache view."""
        if not self.ready:
            raise SourceWorkerError(self._fatal_error or "source extension is unavailable")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"op": "snapshot", "request_id": request_id, "query": query})
            return await asyncio.wait_for(future, timeout=self.ipc_timeout)
        except asyncio.TimeoutError as exc:
            raise SourceWorkerError("source extension snapshot timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def place_details(self, place_id: str) -> dict[str, object]:
        """Ask the source child for a detail record for one retained provider ID."""
        if not isinstance(place_id, str) or not _PLACE_ID.fullmatch(place_id):
            raise SourceWorkerError("selected provider place is invalid")
        if not self.ready:
            raise SourceWorkerError(self._fatal_error or "source extension is unavailable")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending_places[request_id] = future
        try:
            await self._send({"op": "place", "request_id": request_id, "place_id": place_id})
            return await asyncio.wait_for(future, timeout=max(self.ipc_timeout, 12.0))
        except asyncio.TimeoutError as exc:
            raise SourceWorkerError("selected provider place timed out") from exc
        finally:
            self._pending_places.pop(request_id, None)

    async def search(self, q: str, *, scope: str = "world", lat: float | None = None, lng: float | None = None, radius: int | None = None) -> dict[str, object]:
        if not self.ready:
            raise SourceWorkerError(self._fatal_error or "source extension is unavailable")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending_searches[request_id] = future
        try:
            await self._send({"op": "search", "request_id": request_id, "q": q, "scope": scope, "lat": lat, "lng": lng, "radius": radius})
            return await asyncio.wait_for(future, timeout=max(self.ipc_timeout, 12.0))
        except asyncio.TimeoutError as exc:
            raise SourceWorkerError("source extension search timed out") from exc
        finally:
            self._pending_searches.pop(request_id, None)

    def cached_snapshot(self) -> dict[str, object] | None:
        return copy.deepcopy(self._snapshot) if self._snapshot is not None else None

    async def stop(self) -> None:
        """Request child shutdown, then terminate/kill only this bounded child."""
        self._stopping = True
        process = self.process
        if process is not None:
            if process.returncode is None:
                try:
                    await self._send({"op": "stop"})
                except SourceWorkerError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            self.process = None
        if self._reader_task is not None:
            if not self._reader_task.done():
                self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(SourceWorkerError("source extension stopped"))
        self._pending.clear()
        for future in self._pending_places.values():
            if not future.done():
                future.set_exception(SourceWorkerError("source extension stopped"))
        self._pending_places.clear()
        for future in self._pending_searches.values():
            if not future.done(): future.set_exception(SourceWorkerError("source extension stopped"))
        self._pending_searches.clear()
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
        self._ready.clear()
        self._bootstrap.clear()
