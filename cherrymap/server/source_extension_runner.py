#!/usr/bin/env python3
"""Trusted launcher for the original CherryPro source plus mapping extension.

This file is the only child executable started by ``SourceWorkerSupervisor``.
It deliberately does not synthesize, copy, or edit the submitted source.  It
runs the approved bytes as ``__main__`` under bootstrap containment, restores a
private protocol stdout, receives one bounded initialization frame, and only
then attaches ``mapping_extension.run`` in the *same PID*.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import resource
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.source_gate import EXPECTED_SHA256, SOURCE_PATH
from server.source_worker_safety import BootstrapSafety, SourceWorkerSafetyError

MAX_COMMAND_BYTES = 16_384
MAX_LOG_BYTES = 256 * 1024
LAUNCHER_PATH = Path(__file__).resolve()
EXTENSION_PATH = PROJECT_ROOT / "server" / "mapping_extension.py"
EXTENSION_SHA_FILE = EXTENSION_PATH.with_suffix(".py.sha256")
LAUNCHER_SHA_FILE = LAUNCHER_PATH.with_suffix(".py.sha256")
UPDATES_PATH = PROJECT_ROOT / "runtime" / "marker_coordinates.json"


class _BoundedLog:
    """Private scratch diagnostic sink; it never reaches protocol stdout."""

    def __init__(self, path: Path, maximum: int = MAX_LOG_BYTES) -> None:
        self._file = open(path, "w", encoding="utf-8", errors="replace")
        self._maximum = maximum
        self._written = 0

    def write(self, value: object) -> int:
        text = str(value)
        remaining = self._maximum - self._written
        if remaining > 0:
            portion = text[:remaining]
            self._file.write(portion)
            self._file.flush()
            self._written += len(portion)
        return len(text)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _recorded_sha256(path: Path, record: Path) -> str:
    if not path.is_file() or not record.is_file():
        raise RuntimeError("artifact unavailable")
    expected = record.read_text(encoding="ascii").strip().split()[0]
    actual = _sha256(path)
    if len(expected) != 64 or expected != actual:
        raise RuntimeError("artifact fingerprint mismatch")
    return actual


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _emit(protocol_stdout: Any, payload: dict[str, object]) -> None:
    """Emit an intentionally small fixed-shape protocol record."""
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return
    if len(encoded.encode("utf-8")) <= MAX_COMMAND_BYTES:
        protocol_stdout.write(encoded + "\n")
        protocol_stdout.flush()


def _fatal(protocol_stdout: Any, category: str) -> int:
    _emit(protocol_stdout, {"type": "fatal", "category": category})
    return 70


def _read_init(protocol_stdin: Any, *, launcher_sha256: str, extension_sha256: str) -> dict[str, str] | None:
    """Read precisely one bounded JSON initialization frame before asyncio exists."""
    raw = protocol_stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
    if not raw or len(raw) > MAX_COMMAND_BYTES:
        return None
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict) or set(message) != {"op", "config"} or message.get("op") != "init":
        return None
    config = message.get("config")
    expected = {
        "database_url", "maps_api_url", "maps_api_key", "original_source", "original_sha256", "updates_path",
        "launcher_sha256", "extension_sha256",
    }
    if not isinstance(config, dict) or set(config) != expected:
        return None
    limits = {
        "database_url": 4096, "maps_api_url": 2048, "maps_api_key": 4096, "original_source": 1024,
        "original_sha256": 64, "updates_path": 1024, "launcher_sha256": 64, "extension_sha256": 64,
    }
    if any(not isinstance(config.get(key), str) or len(config[key]) > limit for key, limit in limits.items()):
        return None
    if (
        config["original_source"] != str(SOURCE_PATH.resolve())
        or config["original_sha256"] != EXPECTED_SHA256
        or config["updates_path"] != str(UPDATES_PATH.resolve())
        or config["launcher_sha256"] != launcher_sha256
        or config["extension_sha256"] != extension_sha256
    ):
        return None
    return {key: str(config[key]) for key in limits}


def main() -> int:
    if sys.argv[1:] != ["--source-extension"]:
        return 64
    scratch = Path.cwd().resolve()
    if not scratch.is_dir():
        return 65
    protocol_stdout, protocol_stdin = sys.stdout, sys.stdin
    # The parent checks launcher hash before exec; the child independently checks
    # both deployment records before source execution. No remote code path exists.
    try:
        launcher_sha256 = _recorded_sha256(LAUNCHER_PATH, LAUNCHER_SHA_FILE)
        extension_sha256 = _recorded_sha256(EXTENSION_PATH, EXTENSION_SHA_FILE)
        if _sha256(SOURCE_PATH) != EXPECTED_SHA256:
            return _fatal(protocol_stdout, "approved-source-fingerprint-mismatch")
    except Exception:
        return _fatal(protocol_stdout, "extension-fingerprint-unavailable")

    private_log = _BoundedLog(scratch / "bootstrap.stderr.log")
    safety = BootstrapSafety(
        project_root=PROJECT_ROOT,
        original_source=SOURCE_PATH,
        worker_source=LAUNCHER_PATH,
        scratch=scratch,
    )
    body_executed = False
    try:
        # Audit and redirection begin before the original source. Its raw stdout,
        # stderr, and generated reports stay private under scratch.
        safety.begin_bootstrap()
        previous_stdout, previous_stderr = sys.stdout, sys.stderr
        sys.stdout = private_log
        sys.stderr = private_log
        # Match the startup gate: both soft and hard address-space limits stay at
        # 2GB. A soft-only cap can be raised again by the source, which then
        # reserves ~3.6GB and is OOM-killed. The hard ceiling is what makes the
        # gate finish on this host. It cannot be lifted later; the fusion worker
        # runs inside the same proven ceiling.
        address_cap = 2 * 1024**3
        resource.setrlimit(resource.RLIMIT_AS, (address_cap, address_cap))
        try:
            namespace = runpy.run_path(str(SOURCE_PATH), run_name="__main__")
            body_executed = True
        except SystemExit as exc:
            # A normal zero exit still proves the original body completed.
            if exc.code not in (None, 0):
                raise
            namespace = {"__name__": "__main__"}
            body_executed = True
        finally:
            gc.collect()
            try:
                import ctypes
                libc = ctypes.CDLL(None)
                libc.malloc_trim.argtypes = [ctypes.c_size_t]
                libc.malloc_trim.restype = ctypes.c_int
                libc.malloc_trim(0)
            except Exception:
                pass
            gc.collect()
            # The original may alter sys streams. Protocol output is restored
            # explicitly and never inherits source-defined stream variables.
            sys.stdout, sys.stderr = previous_stdout, previous_stderr
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            address = resource.getrlimit(resource.RLIMIT_AS)
            nproc = resource.getrlimit(resource.RLIMIT_NPROC) if hasattr(resource, "RLIMIT_NPROC") else None
            private_log.write(f"post-bootstrap maxrss_kb={usage.ru_maxrss} as={address} nproc={nproc}\n")
        except Exception:
            pass
        # The extension treats the namespace as execution proof only and deletes
        # it immediately. Drop those globals first so a large source allocation
        # cannot pin the 2GB address space and block the fusion worker's threads.
        try:
            namespace.clear()
        except Exception:
            pass
        del namespace
        namespace = {}
        gc.collect()
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.malloc_trim.restype = ctypes.c_int
            libc.malloc_trim(0)
        except Exception:
            pass
        gc.collect()
    except BaseException:
        private_log.close()
        return _fatal(protocol_stdout, "original-source-bootstrap-failed")

    runner = {
        "kind": "cherrypro-source-extension",
        "filename": SOURCE_PATH.name,
        "extension_filename": EXTENSION_PATH.name,
        "pid": os.getpid(),
        "started_at": _now(),
        "sha256": EXPECTED_SHA256,
        "original_sha256": EXPECTED_SHA256,
        "extension_sha256": extension_sha256,
    }
    _emit(protocol_stdout, {"type": "bootstrap-complete", "runner": runner, "body_executed": body_executed})
    config = _read_init(protocol_stdin, launcher_sha256=launcher_sha256, extension_sha256=extension_sha256)
    if config is None:
        private_log.close()
        return _fatal(protocol_stdout, "runtime-initialization-invalid")

    try:
        # Runtime network access is activated only after original execution. DNS
        # and actual connections remain confined to this DB/HTTPS Maps allowlist.
        safety.activate_runtime(database_url=config["database_url"], maps_api_url=config["maps_api_url"])
        os.environ["DATABASE_URL"] = config["database_url"]
        os.environ["MANUS_API_URL"] = config["maps_api_url"]
        os.environ["MANUS_API_KEY"] = config["maps_api_key"]
        from server.mapping_extension import run

        return run(namespace=namespace, config=config, protocol_stdout=protocol_stdout, runner=runner, private_log=private_log)
    except SourceWorkerSafetyError:
        private_log.close()
        return _fatal(protocol_stdout, "runtime-policy-unavailable")
    except BaseException:
        private_log.close()
        return _fatal(protocol_stdout, "mapping-extension-startup-failed")


if __name__ == "__main__":
    raise SystemExit(main())
