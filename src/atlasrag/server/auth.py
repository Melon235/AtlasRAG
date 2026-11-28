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
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

import atlasrag.runtime.ownership as _ownership_module
from atlasrag._canonical import validate_unicode_scalar_text
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
    """Ensure a target base URL uses strict HTTP(S) loopback without unsafe components."""

    cleaned = validate_unicode_scalar_text(url)
    if cleaned != cleaned.strip() or not cleaned:
        raise ValueError("server URL must not be blank or contain outer whitespace")
    parsed = urlsplit(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("server URL must use http or https")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("server URL must not contain userinfo")
    if (
        parsed.query
        or parsed.fragment
        or "?" in cleaned
        or "#" in cleaned
        or ";" in cleaned
    ):
        raise ValueError("server URL must not contain query, fragment, or params")
    if parsed.path not in {"", "/"}:
        raise ValueError("server URL must not contain a non-root path")
    if parsed.hostname is None:
        raise ValueError("server URL must contain a loopback host")
    normalized_host = validate_loopback_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("server URL contains an invalid port") from error
    scheme = parsed.scheme.casefold()
    default_port = 80 if scheme == "http" else 443
    host_formatted = (
        f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    )
    if port is not None and port != default_port:
        return f"{scheme}://{host_formatted}:{port}"
    return f"{scheme}://{host_formatted}"


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
    try:
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_RECEIPT_FILENAME = re.compile(r"^shutdown-([A-Za-z0-9_-]{1,128})\.json$")


class CleanupOutcome(StrEnum):
    """Explicit result of verified instance-owned metadata removal."""

    ABSENT = "ABSENT"
    REMOVED = "REMOVED"
    FOREIGN_INSTANCE = "FOREIGN_INSTANCE"


class MetadataCleanupError(RuntimeError):
    """Raised when owned metadata validation or unlink fails."""


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
    shutdown_timeout_seconds: float | None = None


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


def _inspect_lock_inode(lock_path: Path) -> tuple[int, int] | None:
    """Return (st_dev, st_ino) iff lock_path is secure and actively flock-held."""

    try:
        dir_info = lock_path.parent.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        return None
    if dir_info.st_uid != os.getuid() or (stat.S_IMODE(dir_info.st_mode) & 0o077):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(lock_path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if stat.S_IMODE(info.st_mode) & 0o077:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return (info.st_dev, info.st_ino)
            return None
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        os.close(fd)


def is_runtime_lock_active(lock_path: Path) -> bool:
    """Return True iff runtime.lock is regular, UID-owned, 0o600, and flock-held."""

    return _inspect_lock_inode(lock_path) is not None


def verify_process_holds_flock(pid: int, lock_path: Path) -> bool:
    """Verify via kernel /proc/<pid>/fdinfo that pid holds exclusive FLOCK on lock_path."""

    if pid < 1:
        return False
    inode_pair = _inspect_lock_inode(lock_path)
    if inode_pair is None:
        return False
    st_dev, st_ino = inode_pair
    expected_lock_id = f"{os.major(st_dev):02x}:{os.minor(st_dev):02x}:{st_ino}"

    fd_dir = Path(f"/proc/{pid}/fd")
    fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return False

    for entry in entries:
        try:
            fd_stat = entry.stat()
        except OSError:
            continue
        if (fd_stat.st_dev, fd_stat.st_ino) != (st_dev, st_ino):
            continue
        try:
            info_text = (fdinfo_dir / entry.name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in info_text.splitlines():
            if not line.startswith("lock:"):
                continue
            tokens = line.split()
            # Format: lock: <idx>: FLOCK ADVISORY WRITE <pid> <maj>:<min>:<ino> ...
            if (
                len(tokens) >= 7
                and tokens[2] == "FLOCK"
                and tokens[3] == "ADVISORY"
                and tokens[4] == "WRITE"
                and tokens[5].isdigit()
                and int(tokens[5]) == pid
                and tokens[6] == expected_lock_id
            ):
                return True
    return False


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
    canonical_url = validate_loopback_url(record.url)
    if canonical_url != record.url:
        record = ServerDiscoveryRecord.model_validate(
            {**record.model_dump(mode="python"), "url": canonical_url}
        )
    return record


def verify_target_process(
    state_dir: Path, *, lock_path: Path | None = None
) -> VerifiedTargetProcess:
    """Capture TargetServerIdentity, open pidfd, and verify start time, UID, and lock."""

    target_lock = (
        _ownership_module.canonical_lock_path() if lock_path is None else lock_path
    )
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
        shutdown_timeout_seconds=record.shutdown_timeout_seconds,
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
        if not verify_process_holds_flock(identity.pid, target_lock):
            raise ProcessIdentityVerificationError(
                "target process does not hold canonical runtime ownership lock"
            )
        if read_proc_stat_starttime(identity.pid) != identity.process_start_ticks:
            raise ProcessIdentityVerificationError("process start time changed")
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


def _ensure_absent_or_raise(path: Path, state_dir: Path) -> None:
    if not state_dir.exists():
        raise FileNotFoundError(path)
    dir_info = state_dir.lstat()
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise MetadataCleanupError("state directory must not be a symlink")
    path.lstat()


def unlink_owned_server_discovery(
    state_dir: Path, server_instance_id: str
) -> CleanupOutcome:
    """Fail-closed instance-verified removal of server.json."""

    path = server_discovery_path(state_dir)
    try:
        _ensure_absent_or_raise(path, state_dir)
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("server.json path inspection failed") from error

    try:
        raw = read_secure_local_file(path, max_bytes=16_384)
        record = ServerDiscoveryRecord.model_validate_json(raw)
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except (OSError, PermissionError, ValueError, ValidationError) as error:
        raise MetadataCleanupError(
            "server.json failed security or schema checks"
        ) from error

    if record.server_instance_id != server_instance_id:
        return CleanupOutcome.FOREIGN_INSTANCE

    try:
        path.unlink()
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("failed to unlink owned server.json") from error
    return CleanupOutcome.REMOVED


def unlink_owned_control_token(state_dir: Path, expected_token: str) -> CleanupOutcome:
    """Fail-closed token-verified removal of control.token."""

    if not expected_token:
        raise MetadataCleanupError("empty expected control token")
    path = control_token_path(state_dir)
    try:
        _ensure_absent_or_raise(path, state_dir)
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("control.token path inspection failed") from error

    try:
        current = read_secure_local_file(path, max_bytes=8_192).strip()
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except (OSError, PermissionError, ValueError) as error:
        raise MetadataCleanupError("control.token failed security checks") from error

    if not hmac.compare_digest(current.encode("utf-8"), expected_token.encode("utf-8")):
        return CleanupOutcome.FOREIGN_INSTANCE

    try:
        path.unlink()
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("failed to unlink owned control.token") from error
    return CleanupOutcome.REMOVED


def unlink_owned_shutdown_receipt(
    state_dir: Path, server_instance_id: str
) -> CleanupOutcome:
    """Unlink owned clean-shutdown receipt only on pre-release rollback."""

    path = shutdown_receipt_path(state_dir, server_instance_id)
    try:
        _ensure_absent_or_raise(path, state_dir)
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("shutdown receipt inspection failed") from error

    try:
        raw = read_secure_local_file(path, max_bytes=16_384)
        receipt = ShutdownReceipt.model_validate_json(raw)
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except (OSError, PermissionError, ValueError, ValidationError) as error:
        raise MetadataCleanupError(
            "shutdown receipt failed security or schema checks"
        ) from error

    if receipt.server_instance_id != server_instance_id:
        return CleanupOutcome.FOREIGN_INSTANCE

    try:
        path.unlink()
    except FileNotFoundError:
        return CleanupOutcome.ABSENT
    except OSError as error:
        raise MetadataCleanupError("failed to unlink shutdown receipt") from error
    return CleanupOutcome.REMOVED
