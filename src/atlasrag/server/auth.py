"""Local transport security, loopback validation, and safe state-file helpers."""

from __future__ import annotations

import errno
import fcntl
import hmac
import os
import pwd
import re
import secrets
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from atlasrag.runtime.ownership import canonical_lock_path
from atlasrag.server.config import ServerConfig, validate_loopback_host
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from atlasrag.server.schemas import ServerDiscoveryRecord, ShutdownReceipt

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_CLIENT_CLASSIFICATIONS = frozenset({"web", "cli"})


def canonical_state_directory(server_config: ServerConfig | None = None) -> Path:
    """Return the local per-user AtlasRAG state directory."""

    if server_config is not None and server_config.state_directory is not None:
        return Path(server_config.state_directory)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "atlasrag"


def validate_loopback_url(url: str) -> str:
    """Ensure a target URL uses HTTP(S) on loopback without userinfo."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("server URL must use http or https")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("server URL must not contain userinfo")
    if parsed.hostname is None:
        raise ValueError("server URL must contain a loopback host")
    validate_loopback_host(parsed.hostname)
    return url.rstrip("/")


def build_allowed_hosts(config: ServerConfig) -> frozenset[str]:
    """Build exact Host header allowlist values for loopback binding."""

    if config.allowed_hosts is not None:
        return frozenset(item.casefold() for item in config.allowed_hosts)
    port = config.port
    candidates = {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    }
    if config.host == "::1":
        candidates.add(f"[::1]:{port}")
    elif config.host not in {"127.0.0.1", "localhost"}:
        candidates.add(f"{config.host.casefold()}:{port}")
    return frozenset(candidates)


def build_allowed_origins(config: ServerConfig) -> frozenset[str]:
    """Build exact same-origin values matching the loopback server."""

    origins = {
        f"http://127.0.0.1:{config.port}",
        f"http://localhost:{config.port}",
        f"http://[::1]:{config.port}",
    }
    if config.allowed_hosts is not None:
        for host_entry in config.allowed_hosts:
            origins.add(f"http://{host_entry.casefold()}")
    return frozenset(origins)


def validate_host_header(
    host_header: str | None, allowed_hosts: frozenset[str]
) -> None:
    """Reject missing or non-allowlisted Host headers (DNS rebinding guard)."""

    if not host_header:
        raise ServerHttpError(TransportErrorCode.FORBIDDEN)
    normalized = host_header.strip().casefold()
    if normalized not in allowed_hosts:
        raise ServerHttpError(TransportErrorCode.FORBIDDEN)


def _origin_from_referer(referer: str) -> str | None:
    parsed = urlsplit(referer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def validate_origin_and_referer(
    *,
    method: str,
    origin: str | None,
    referer: str | None,
    allowed_origins: frozenset[str],
) -> None:
    """Validate browser Origin/Referer on state-changing HTTP methods."""

    if method.upper() not in _STATE_CHANGING_METHODS:
        return
    if origin is not None:
        normalized_origin = origin.strip().casefold()
        if not normalized_origin or normalized_origin == "null":
            raise ServerHttpError(TransportErrorCode.FORBIDDEN)
        if normalized_origin not in allowed_origins:
            raise ServerHttpError(TransportErrorCode.FORBIDDEN)
        return
    if referer is not None:
        ref_origin = _origin_from_referer(referer.strip())
        if ref_origin is None or ref_origin not in allowed_origins:
            raise ServerHttpError(TransportErrorCode.FORBIDDEN)


def validate_client_csrf_header(client_header: str | None) -> str:
    """Classify client type and guard browser endpoints from simple-form CSRF.

    X-AtlasRAG-Client is a browser CSRF / classification check, never auth.
    """

    if client_header is None:
        raise ServerHttpError(TransportErrorCode.FORBIDDEN)
    normalized = client_header.strip().casefold()
    if normalized not in _ALLOWED_CLIENT_CLASSIFICATIONS:
        raise ServerHttpError(TransportErrorCode.FORBIDDEN)
    return normalized


def validate_bearer_token(
    authorization_header: str | None, expected_token: str | None
) -> None:
    """Validate privileged Control Plane Bearer token in constant time."""

    if not expected_token:
        raise ServerHttpError(TransportErrorCode.UNAUTHORIZED)
    if not authorization_header:
        raise ServerHttpError(TransportErrorCode.UNAUTHORIZED)
    scheme, _, token = authorization_header.strip().partition(" ")
    if scheme.casefold() != "bearer" or not token:
        raise ServerHttpError(TransportErrorCode.UNAUTHORIZED)
    if not hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8")):
        raise ServerHttpError(TransportErrorCode.UNAUTHORIZED)


def ensure_secure_state_directory(directory: Path) -> Path:
    """Create and validate a 0o700 non-symlink directory owned by current UID."""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("state directory must be a non-symlink directory")
    if info.st_uid != os.getuid() or (stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError("state directory permissions or ownership unsafe")
    return directory


def read_secure_local_file(path: Path, *, max_bytes: int = 8_192) -> str:
    """Read a regular, 0o600, current-UID file without following symlinks or blocking."""

    dir_info = path.parent.lstat()
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise PermissionError("state parent directory must not be a symlink")
    if dir_info.st_uid != os.getuid() or (stat.S_IMODE(dir_info.st_mode) & 0o077):
        raise PermissionError("state parent directory permissions or ownership unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("state file must be a regular file")
        if info.st_uid != os.getuid():
            raise PermissionError("state file owner mismatch")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(
                "state file permissions must forbid group/other access"
            )
        if info.st_size > max_bytes:
            raise ValueError("state file exceeds size bound")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, (max_bytes + 1) - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            raise ValueError("state file exceeds size bound")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def write_secure_local_file(path: Path, content: str) -> None:
    """Atomically write a 0o600 regular file inside a verified 0o700 directory."""

    ensure_secure_state_directory(path.parent)
    tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    tmp_path = path.parent / tmp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        encoded = content.encode("utf-8")
        view = memoryview(encoded)
        written = 0
        while written < len(encoded):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, path)


_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_RECEIPT_FILENAME = re.compile(r"^shutdown-([A-Za-z0-9_-]{1,128})\.json$")


class ProcessIdentityVerificationError(RuntimeError):
    """Raised when local server process identity or lock ownership cannot be verified."""


@dataclass(frozen=True, slots=True)
class TargetServerIdentity:
    """Immutable pre-shutdown server identity captured before HTTP/signal actions."""

    server_instance_id: str
    pid: int
    process_start_ticks: int
    boot_id: str
    url: str


@dataclass(slots=True)
class VerifiedTargetProcess:
    """Live pidfd handle bound to a verified TargetServerIdentity."""

    identity: TargetServerIdentity
    pidfd: int | None

    def close(self) -> None:
        if self.pidfd is not None:
            fd = self.pidfd
            self.pidfd = None
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> VerifiedTargetProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _validate_instance_id(server_instance_id: str) -> str:
    if not _SAFE_INSTANCE_ID.fullmatch(server_instance_id):
        raise ValueError("invalid server_instance_id")
    return server_instance_id


def server_discovery_path(state_dir: Path) -> Path:
    return state_dir / "server.json"


def control_token_path(state_dir: Path) -> Path:
    return state_dir / "control.token"


def shutdown_receipt_path(state_dir: Path, server_instance_id: str) -> Path:
    safe_id = _validate_instance_id(server_instance_id)
    return state_dir / f"shutdown-{safe_id}.json"


def cli_session_path(state_dir: Path) -> Path:
    return state_dir / "cli_session.json"


def resolve_runtime_lock_path(server_config: ServerConfig | None = None) -> Path:
    """Return runtime.lock in the explicit state directory or the canonical user lock path."""

    if server_config is not None and server_config.state_directory is not None:
        return Path(server_config.state_directory) / "runtime.lock"
    return canonical_lock_path()


def read_proc_stat_starttime(pid: int) -> int:
    """Read Linux process start time ticks (field 22) from /proc/<pid>/stat."""

    if pid < 1:
        raise ProcessIdentityVerificationError("invalid pid")
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProcessIdentityVerificationError(f"cannot read {stat_path}") from error
    rparen = raw.rfind(")")
    if rparen < 0:
        raise ProcessIdentityVerificationError("malformed /proc stat payload")
    fields = raw[rparen + 2 :].split()
    # Field 3 (state) is index 0; field 22 (starttime) is index 19.
    if len(fields) <= 19:
        raise ProcessIdentityVerificationError("truncated /proc stat payload")
    try:
        return int(fields[19])
    except ValueError as error:
        raise ProcessIdentityVerificationError(
            "invalid /proc stat starttime"
        ) from error


def read_proc_uid(pid: int) -> int:
    """Read real UID of a live Linux process from /proc/<pid>/status."""

    if pid < 1:
        raise ProcessIdentityVerificationError("invalid pid")
    status_path = Path(f"/proc/{pid}/status")
    try:
        raw = status_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProcessIdentityVerificationError(f"cannot read {status_path}") from error
    for line in raw.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError as error:
                    raise ProcessIdentityVerificationError(
                        "invalid /proc Uid"
                    ) from error
    raise ProcessIdentityVerificationError("missing Uid in /proc status")


def read_boot_id() -> str:
    """Read kernel boot UUID from /proc/sys/kernel/random/boot_id."""

    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        boot_id = boot_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ProcessIdentityVerificationError("cannot read kernel boot_id") from error
    if not boot_id:
        raise ProcessIdentityVerificationError("empty kernel boot_id")
    return boot_id


def is_runtime_lock_active(lock_path: Path) -> bool:
    """Return True iff runtime.lock is regular, UID-owned, 0o600, and flock-held."""

    try:
        dir_info = lock_path.parent.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        return False
    if dir_info.st_uid != os.getuid() or (stat.S_IMODE(dir_info.st_mode) & 0o077):
        return False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(lock_path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        if stat.S_IMODE(info.st_mode) & 0o077:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return True
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def load_server_discovery(state_dir: Path) -> ServerDiscoveryRecord:
    """Safely load and validate server.json without following symlinks."""

    discovery_path = server_discovery_path(state_dir)
    if not state_dir.exists():
        raise FileNotFoundError(discovery_path)
    dir_info = state_dir.lstat()
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise PermissionError("state directory must not be a symlink")
    try:
        discovery_path.lstat()
    except FileNotFoundError:
        raise
    raw = read_secure_local_file(discovery_path, max_bytes=16_384)
    record = ServerDiscoveryRecord.model_validate_json(raw)
    validate_loopback_url(record.url)
    return record


def verify_target_process(state_dir: Path, *, lock_path: Path) -> VerifiedTargetProcess:
    """Capture TargetServerIdentity, open pidfd, and verify start time, UID, and lock."""

    try:
        record = load_server_discovery(state_dir)
    except FileNotFoundError:
        raise
    except (OSError, PermissionError, ValueError, ValidationError) as error:
        raise ProcessIdentityVerificationError(
            "server discovery file is missing, unreadable, or invalid"
        ) from error

    identity = TargetServerIdentity(
        server_instance_id=record.server_instance_id,
        pid=record.pid,
        process_start_ticks=record.process_start_ticks,
        boot_id=record.boot_id,
        url=record.url,
    )
    current_boot_id = read_boot_id()
    if identity.boot_id != current_boot_id:
        raise ProcessIdentityVerificationError("kernel boot_id mismatch")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ProcessIdentityVerificationError("pidfd process signalling unavailable")

    try:
        pidfd = os.pidfd_open(identity.pid, 0)
    except OSError as error:
        raise ProcessIdentityVerificationError("unable to open target pidfd") from error

    try:
        actual_ticks = read_proc_stat_starttime(identity.pid)
        if actual_ticks != identity.process_start_ticks:
            raise ProcessIdentityVerificationError("process start time mismatch")
        actual_uid = read_proc_uid(identity.pid)
        if actual_uid != os.getuid():
            raise ProcessIdentityVerificationError("process owner UID mismatch")
        if not is_runtime_lock_active(lock_path):
            raise ProcessIdentityVerificationError(
                "runtime ownership lock is not active"
            )
    except BaseException:
        os.close(pidfd)
        raise
    return VerifiedTargetProcess(identity=identity, pidfd=pidfd)


def load_clean_shutdown_receipt(
    state_dir: Path, identity: TargetServerIdentity
) -> ShutdownReceipt | None:
    """Read and validate shutdown-<server_instance_id>.json for the target instance."""

    receipt_path = shutdown_receipt_path(state_dir, identity.server_instance_id)
    try:
        raw = read_secure_local_file(receipt_path, max_bytes=16_384)
        receipt = ShutdownReceipt.model_validate_json(raw)
    except FileNotFoundError:
        return None
    except (OSError, PermissionError, ValueError, ValidationError):
        return None
    if (
        receipt.server_instance_id != identity.server_instance_id
        or receipt.pid != identity.pid
        or receipt.status != "CLEAN_SHUTDOWN"
    ):
        return None
    return receipt


def prune_shutdown_receipts(state_dir: Path, *, keep: int = 8) -> None:
    """Prune stale shutdown-*.json receipts beyond latest keep count (under lock)."""

    if keep < 0 or not state_dir.exists():
        return
    entries: list[tuple[int, str, Path]] = []
    for candidate in state_dir.iterdir():
        if not _SAFE_RECEIPT_FILENAME.fullmatch(candidate.name):
            continue
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        if info.st_uid != os.getuid():
            continue
        entries.append((info.st_mtime_ns, candidate.name, candidate))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, old_path in entries[keep:]:
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass


def unlink_owned_server_discovery(state_dir: Path, server_instance_id: str) -> None:
    """Unlink server.json only if it safely belongs to server_instance_id."""

    path = server_discovery_path(state_dir)
    try:
        raw = read_secure_local_file(path, max_bytes=16_384)
        record = ServerDiscoveryRecord.model_validate_json(raw)
    except (FileNotFoundError, OSError, PermissionError, ValueError, ValidationError):
        return
    if record.server_instance_id != server_instance_id:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def unlink_owned_control_token(state_dir: Path, expected_token: str) -> None:
    """Unlink control.token only if its verified content matches expected_token."""

    if not expected_token:
        return
    path = control_token_path(state_dir)
    try:
        current = read_secure_local_file(path, max_bytes=8_192).strip()
    except (FileNotFoundError, OSError, PermissionError, ValueError):
        return
    if not hmac.compare_digest(current.encode("utf-8"), expected_token.encode("utf-8")):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
