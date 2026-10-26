"""Minimal child-process runner for the immutable source startup gate.

This is a best-effort Python audit-hook containment layer, not an OS-grade sandbox.
It intentionally starts before the artifact is loaded and fails closed on audited
network, child-process, or out-of-policy filesystem operations.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import runpy
import site
import sys
import sysconfig
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - Unix production image supplies resource
    resource = None  # type: ignore[assignment]


class AuditDenied(PermissionError):
    """Raised for any audited capability that is outside the source gate policy."""


def _resolved(value: object, cwd: Path) -> Path | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        raw = Path(value)
        if not raw.is_absolute():
            raw = cwd / raw
        return raw.resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _runtime_read_roots() -> tuple[Path, ...]:
    candidates: set[Path] = set()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = paths.get(key)
        if value:
            candidates.add(Path(value).resolve(strict=False))
    for value in site.getsitepackages():
        candidates.add(Path(value).resolve(strict=False))
    # Dynamic modules and the interpreter's shared libraries live here.  These
    # are system runtime locations, not the application checkout or user home.
    for value in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"):
        path = Path(value)
        if path.exists():
            candidates.add(path.resolve())
    return tuple(candidates)


def _install_limits() -> None:
    if resource is None:
        return
    # CPU is independent of the parent wall-time limit.  Memory permits numpy
    # imports while preventing an unbounded child address space.
    resource.setrlimit(resource.RLIMIT_CPU, (40, 41))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_NOFILE, (96, 96))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    if hasattr(resource, "RLIMIT_CORE"):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _install_audit_guard(source: Path, scratch: Path) -> None:
    source = source.resolve()
    scratch = scratch.resolve()
    runtime_roots = _runtime_read_roots()
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL

    def allowed_read(path: Path) -> bool:
        return path == source or _is_within(path, scratch) or any(_is_within(path, root) for root in runtime_roots)

    def allowed_write(path: Path) -> bool:
        return _is_within(path, scratch)

    def check_path(value: object, *, writing: bool = False) -> None:
        path = _resolved(value, scratch)
        if path is None:
            raise AuditDenied("unresolvable filesystem operation")
        if writing:
            if not allowed_write(path):
                raise AuditDenied("filesystem write denied")
        elif not allowed_read(path):
            raise AuditDenied("filesystem read denied")

    def audit(event: str, args: tuple[object, ...]) -> None:
        # All socket events include construction and DNS resolution, so source
        # DNS attempts fail before an address is contacted.
        if event.startswith("socket."):
            raise AuditDenied("network access denied")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"}:
            raise AuditDenied("process launch denied")
        if event == "open":
            value = args[0] if args else None
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            check_path(value, writing=bool(flags & write_flags) or any(flag in mode for flag in "wax+"))
        elif event in {"os.remove", "os.unlink", "os.mkdir", "os.rmdir"}:
            check_path(args[0] if args else None, writing=True)
        elif event in {"os.rename", "os.replace"}:
            check_path(args[0] if args else None, writing=True)
            check_path(args[1] if len(args) > 1 else None, writing=True)
        elif event in {"os.chdir", "os.listdir", "os.scandir"}:
            # Python represents os.listdir()/os.scandir() of the current
            # directory as None in the audit event.  The child cwd is scratch.
            value = scratch if (args and args[0] is None) else (args[0] if args else None)
            check_path(value, writing=event == "os.chdir")
        elif event == "ctypes.dlopen":
            # ctypes.CDLL(None) is the interpreter's existing process image,
            # not a filesystem read. Every named library remains path-checked.
            if args and args[0] is not None:
                check_path(args[0])

    sys.addaudithook(audit)


def _confine_write_opens(source: Path, scratch: Path) -> None:
    """Redirect hard-coded report outputs into scratch without changing source.

    The submitted portable source writes a fixed set of report names under the
    invoking user's home.  The runner makes the *actual* target a stable,
    private file under scratch. Reads outside the approved roots still fail and
    no source-originated write reaches its requested external pathname.
    """
    source = source.resolve()
    scratch = scratch.resolve()
    runtime_roots = _runtime_read_roots()
    original_open = builtins.open
    original_os_open = os.open
    original_makedirs = os.makedirs
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL

    def read_allowed(path: Path) -> bool:
        return path == source or _is_within(path, scratch) or any(_is_within(path, root) for root in runtime_roots)

    def redirected(path: Path) -> Path:
        suffix = path.suffix if path.suffix and len(path.suffix) <= 12 else ""
        token = hashlib.sha256(os.fsencode(str(path))).hexdigest()
        target = scratch / "reports" / f"{token}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def confined_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        path = _resolved(file, scratch)
        writing = any(flag in mode for flag in "wax+")
        if path is None:
            return original_open(file, mode, *args, **kwargs)  # type: ignore[arg-type]
        if writing and not _is_within(path, scratch):
            return original_open(redirected(path), mode, *args, **kwargs)
        if not writing and not read_allowed(path):
            raise AuditDenied("filesystem read denied")
        return original_open(path, mode, *args, **kwargs)

    def confined_os_open(file: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        path = _resolved(file, scratch)
        writing = bool(flags & write_flags)
        if path is None:
            return original_os_open(file, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if writing and not _is_within(path, scratch):
            return original_os_open(redirected(path), flags, mode)
        if not writing and not read_allowed(path):
            raise AuditDenied("filesystem read denied")
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    def confined_makedirs(name: object, mode: int = 0o777, exist_ok: bool = False) -> None:
        path = _resolved(name, scratch)
        if path is None:
            raise AuditDenied("unresolvable filesystem operation")
        if not _is_within(path, scratch):
            original_makedirs(redirected(path), mode=mode, exist_ok=True)
            return
        original_makedirs(path, mode=mode, exist_ok=exist_ok)

    builtins.open = confined_open  # type: ignore[assignment]
    os.open = confined_os_open  # type: ignore[assignment]
    os.makedirs = confined_makedirs  # type: ignore[assignment]


def main() -> int:
    if len(sys.argv) != 2:
        return 64
    source = Path(sys.argv[1])
    scratch = Path.cwd()
    if not source.is_file() or not scratch.is_dir():
        return 65
    try:
        # This runner-owned sink is opened before the source audit hook is
        # active; the artifact itself is never granted access to /dev/null.
        null = open(os.devnull, "w", encoding="utf-8")
        _install_limits()
        _install_audit_guard(source, scratch)
        _confine_write_opens(source, scratch)
        sys.dont_write_bytecode = True
        # Source output is deliberately discarded: generated files are counted
        # by the parent but raw logs never cross into the web application.
        with null:
            previous_stdout, previous_stderr = sys.stdout, sys.stderr
            sys.stdout = null
            sys.stderr = null
            try:
                runpy.run_path(str(source), run_name="__main__")
            finally:
                sys.stdout, sys.stderr = previous_stdout, previous_stderr
    except BaseException:
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
