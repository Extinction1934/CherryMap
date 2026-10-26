"""Hash verification and isolated execution for the mandatory source gate."""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .source_locations import count_disabled_main_guards, extract_approved_locations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_ROOT / "source" / "CherryPro_Z4_portable.py"
HASH_FILE = PROJECT_ROOT / "source" / "CherryPro_Z4_portable.py.sha256"
RUNNER_PATH = PROJECT_ROOT / "server" / "restricted_runner.py"
EXPECTED_SHA256 = "c57050653ad7a4ad9ad855d141136fba4d79413656334a30ce59056af64d6fe4"
DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_TIMEOUT_SECONDS = 120.0


class SourceGateError(RuntimeError):
    """Startup must stop when immutable source verification/execution fails."""


@dataclass(frozen=True)
class SourceGateResult:
    status: str
    filename: str
    sha256: str
    python_version: str
    executed_at: str
    duration_ms: int
    exit_code: int
    report_count: int
    main_guards_disabled: int
    location_count: int
    network_access: bool

    def public_dict(self) -> dict[str, str | int | bool]:
        return {
            "status": self.status,
            "filename": self.filename,
            "sha256": self.sha256,
            "python_version": self.python_version,
            "executed_at": self.executed_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "report_count": self.report_count,
            "main_guards_disabled": self.main_guards_disabled,
            "location_count": self.location_count,
            "network_access": self.network_access,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_hash() -> str:
    try:
        stored = HASH_FILE.read_text(encoding="ascii").strip().split()[0]
    except (OSError, IndexError, UnicodeDecodeError) as exc:
        raise SourceGateError("source fingerprint record is unavailable") from exc
    if stored != EXPECTED_SHA256:
        raise SourceGateError("source fingerprint record is invalid")
    return stored


def _child_environment(scratch: Path) -> dict[str, str]:
    """The source child receives no inherited configuration or API credentials."""
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
    }


def _count_regular_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def run_source_gate(
    *,
    source_path: Path = SOURCE_PATH,
    expected_sha256: str | None = None,
    runner_path: Path = RUNNER_PATH,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    validate_metadata: bool = True,
) -> SourceGateResult:
    """Verify and execute source once. Any gate issue raises before web readiness."""
    expected = expected_sha256 or _expected_hash()
    if not source_path.is_file():
        raise SourceGateError("approved source artifact is unavailable")
    actual = _sha256(source_path)
    if actual != expected:
        raise SourceGateError("approved source fingerprint mismatch")
    if not runner_path.is_file():
        raise SourceGateError("restricted source runner is unavailable")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise SourceGateError("source timeout configuration is invalid")

    started = time.monotonic()
    completed_at = ""
    with tempfile.TemporaryDirectory(prefix="cherrymap-source-") as scratch_string:
        scratch = Path(scratch_string)
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(runner_path), str(source_path.resolve())],
                cwd=scratch,
                env=_child_environment(scratch),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceGateError("approved source execution timed out") from exc
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if completed.returncode != 0:
            raise SourceGateError(f"approved source execution failed (exit code {completed.returncode})")
        report_count = _count_regular_files(scratch)

    locations = extract_approved_locations(source_path) if validate_metadata else []
    guards = count_disabled_main_guards(source_path) if validate_metadata else 0
    return SourceGateResult(
        status="passed",
        filename=source_path.name,
        sha256=actual,
        python_version=platform.python_version(),
        executed_at=completed_at,
        duration_ms=duration_ms,
        exit_code=0,
        report_count=report_count,
        main_guards_disabled=guards,
        location_count=len(locations),
        network_access=False,
    )
