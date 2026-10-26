"""Best-effort process-local containment for the source-extension child.

The launcher installs this guard before executing the immutable original.  The
bootstrap phase redirects source writes to private scratch and denies networking
and process launch.  Only after that exact execution completes can runtime move
to a narrow managed-DB/HTTPS-Maps connection allowlist.  This is defense in
depth, not an OS sandbox.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import os
import site
import socket
import sys
import sysconfig
from pathlib import Path
from urllib.parse import urlsplit

try:
    import resource
except ImportError:  # pragma: no cover - Unix production image has resource
    resource = None  # type: ignore[assignment]


class SourceWorkerSafetyError(RuntimeError):
    """A bootstrap/runtime capability policy could not be established."""


class AuditDenied(PermissionError):
    """Raised by the process-local audit hook for a forbidden capability."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(value: object, cwd: Path) -> Path | None:
    if isinstance(value, int) or value is None or not isinstance(value, (str, bytes, os.PathLike)):
        return None
    try:
        path = Path(value)
        return (path if path.is_absolute() else cwd / path).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return None


def _system_read_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        if paths.get(key):
            roots.add(Path(paths[key]).resolve(strict=False))
    for item in site.getsitepackages():
        roots.add(Path(item).resolve(strict=False))
    # TLS needs CA files; timezone and passwd are exact libc/Python read-only
    # dependencies, not general /etc access.
    for item in ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/etc/ssl", "/etc/pki", "/etc/localtime", "/etc/passwd"):
        path = Path(item)
        if path.exists():
            roots.add(path.resolve(strict=False))
    return tuple(roots)


class BootstrapSafety:
    """Mutable audit policy with bootstrap denial and runtime endpoint allowlist."""

    # These are fixed in source, use HTTPS/443 only, and are activated only
    # after the immutable source body has completed. They serve the bounded POI
    # prototype; arbitrary client-selected URLs are never permitted.
    PUBLIC_OVERPASS_HOSTS = ("api.openstreetmap.org", "overpass.openstreetmap.fr", "overpass.private.coffee")

    def __init__(self, *, project_root: Path, original_source: Path, worker_source: Path, scratch: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.original_source = Path(original_source).resolve()
        self.worker_source = Path(worker_source).resolve()
        self.scratch = Path(scratch).resolve()
        self.server_root = (self.project_root / "server").resolve()
        self.runtime_updates = (self.project_root / "runtime" / "marker_coordinates.json").resolve()
        self._system_roots = _system_read_roots()
        self._phase = "bootstrap"
        self._allowed_hosts: dict[str, set[int]] = {}
        self._allowed_ips: dict[str, set[int]] = {}
        self._installed = False
        self._original_open = builtins.open
        self._original_io_open = io.open
        self._original_os_open = os.open
        self._original_makedirs = os.makedirs

    @property
    def phase(self) -> str:
        return self._phase

    def begin_bootstrap(self) -> None:
        """Install redirect/audit policy before original bytes are executed."""
        if self._installed:
            return
        self.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._install_limits()
        self._install_file_redirects()
        sys.addaudithook(self._audit)
        self._installed = True

    @staticmethod
    def _install_limits() -> None:
        if resource is None:
            return
        # A total CPU cap would kill an intended continuous child after minutes.
        # Keep descriptor/core limits, while parent lifespan/stop bounds process life.
        resource.setrlimit(resource.RLIMIT_NOFILE, (96, 96))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def _allowed_read(self, path: Path) -> bool:
        if path in {self.original_source, self.worker_source, self.runtime_updates}:
            return True
        if _inside(path, self.scratch):
            return True
        # Extension and its trusted dependencies are code under server; arbitrary
        # checkout files, user homes, .env files, and source reports are excluded.
        if _inside(path, self.server_root) and path.suffix in {".py", ".pyc"}:
            return True
        return any(path == root or _inside(path, root) for root in self._system_roots)

    def _redirected_report(self, path: Path) -> Path:
        suffix = path.suffix if 0 < len(path.suffix) <= 12 else ""
        token = hashlib.sha256(os.fsencode(str(path))).hexdigest()
        target = self.scratch / "reports" / f"{token}{suffix}"
        self._original_makedirs(target.parent, mode=0o700, exist_ok=True)
        return target

    def _install_file_redirects(self) -> None:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL

        def confined_open(file: object, mode: str = "r", *args: object, **kwargs: object):
            path = _resolve(file, self.scratch)
            writing = any(flag in mode for flag in "wax+")
            if path is None:
                return self._original_open(file, mode, *args, **kwargs)  # type: ignore[arg-type]
            if writing and not _inside(path, self.scratch):
                path = self._redirected_report(path)
            elif not writing and not self._allowed_read(path):
                raise AuditDenied("filesystem read denied")
            return self._original_open(path, mode, *args, **kwargs)

        def confined_io_open(file: object, mode: str = "r", *args: object, **kwargs: object):
            path = _resolve(file, self.scratch)
            writing = any(flag in mode for flag in "wax+")
            if path is None:
                return self._original_io_open(file, mode, *args, **kwargs)  # type: ignore[arg-type]
            if writing and not _inside(path, self.scratch):
                path = self._redirected_report(path)
            elif not writing and not self._allowed_read(path):
                raise AuditDenied("filesystem read denied")
            return self._original_io_open(path, mode, *args, **kwargs)

        def confined_os_open(file: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            path = _resolve(file, self.scratch)
            writing = bool(flags & write_flags)
            if path is None:
                return self._original_os_open(file, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
            if writing and not _inside(path, self.scratch):
                path = self._redirected_report(path)
            elif not writing and not self._allowed_read(path):
                raise AuditDenied("filesystem read denied")
            return self._original_os_open(path, flags, mode)

        def confined_makedirs(name: object, mode: int = 0o777, exist_ok: bool = False) -> None:
            path = _resolve(name, self.scratch)
            if path is None:
                raise AuditDenied("unresolvable filesystem operation")
            if not _inside(path, self.scratch):
                path = self._redirected_report(path)
            self._original_makedirs(path, mode=mode, exist_ok=exist_ok)

        builtins.open = confined_open  # type: ignore[assignment]
        io.open = confined_io_open  # type: ignore[assignment]
        os.open = confined_os_open  # type: ignore[assignment]
        os.makedirs = confined_makedirs  # type: ignore[assignment]

    @staticmethod
    def _host_port(url: str, *, require_https: bool = False) -> tuple[str, int]:
        parsed = urlsplit(url)
        if not parsed.hostname:
            raise SourceWorkerSafetyError("runtime endpoint is invalid")
        if require_https and parsed.scheme != "https":
            raise SourceWorkerSafetyError("runtime endpoint is invalid")
        if parsed.scheme not in {"https", "mysql", "mysql+pymysql"}:
            raise SourceWorkerSafetyError("runtime endpoint is invalid")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 3306)
        except ValueError as exc:
            raise SourceWorkerSafetyError("runtime endpoint is invalid") from exc
        if not 1 <= port <= 65535:
            raise SourceWorkerSafetyError("runtime endpoint is invalid")
        return parsed.hostname.casefold(), port

    def activate_runtime(self, *, database_url: str, maps_api_url: str) -> None:
        """Permit DB/maps plus the two fixed HTTPS OpenStreetMap POI endpoints."""
        if not self._installed or self._phase != "bootstrap":
            raise SourceWorkerSafetyError("runtime policy transition is invalid")
        endpoints: dict[str, set[int]] = {}
        if database_url:
            db_host, db_port = self._host_port(database_url)
            endpoints.setdefault(db_host, set()).add(db_port)
        if maps_api_url:
            maps_host, maps_port = self._host_port(maps_api_url, require_https=True)
            if maps_port != 443:
                raise SourceWorkerSafetyError("runtime endpoint is invalid")
            endpoints.setdefault(maps_host, set()).add(maps_port)
        for host in self.PUBLIC_OVERPASS_HOSTS:
            endpoints.setdefault(host, set()).add(443)
        self._allowed_hosts = endpoints
        self._phase = "runtime"
        for host, ports in self._allowed_hosts.items():
            try:
                addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except OSError:
                continue
            for address in addresses:
                self._allowed_ips.setdefault(str(address[4][0]), set()).update(ports)

    def _network_allowed(self, event: str, args: tuple[object, ...]) -> bool:
        if self._phase != "runtime":
            return False
        if event == "socket.getaddrinfo":
            host = args[0] if args else None
            return isinstance(host, str) and host.casefold() in self._allowed_hosts
        if event in {"socket.connect", "socket.connect_ex"}:
            address = args[1] if len(args) > 1 else None
            if not isinstance(address, tuple) or len(address) < 2:
                return False
            host, port = str(address[0]), address[1]
            return isinstance(port, int) and port in self._allowed_ips.get(host, set())
        if event == "socket.__new__":
            # asyncio's internal wakeup uses a local AF_UNIX socketpair. Remote
            # socket construction alone is not an egress capability; later
            # getaddrinfo/connect checks enforce exact host/IP/port policy.
            return True
        # bind/listen/accept/sendto and all other socket actions are forbidden.
        return False

    def _audit(self, event: str, args: tuple[object, ...]) -> None:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL
        if event.startswith("socket."):
            if not self._network_allowed(event, args):
                raise AuditDenied("network access denied")
            return
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec"}:
            raise AuditDenied("process launch denied")
        if event == "open":
            path = _resolve(args[0] if args else None, self.scratch)
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & write_flags) or any(flag in mode for flag in "wax+")
            if path is None or (writing and not _inside(path, self.scratch)) or (not writing and not self._allowed_read(path)):
                raise AuditDenied("filesystem access denied")
        elif event in {"os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.rename", "os.replace"}:
            paths = args[:2] if event in {"os.rename", "os.replace"} else args[:1]
            if any((path := _resolve(item, self.scratch)) is None or not _inside(path, self.scratch) for item in paths):
                raise AuditDenied("filesystem write denied")
        elif event in {"os.chdir", "os.listdir", "os.scandir"}:
            item = self.scratch if args and args[0] is None else (args[0] if args else None)
            path = _resolve(item, self.scratch)
            if path is None or not self._allowed_read(path):
                raise AuditDenied("filesystem access denied")
