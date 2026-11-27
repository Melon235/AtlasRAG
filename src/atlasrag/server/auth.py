"""Local transport security, loopback validation, and safe state-file helpers."""

from __future__ import annotations

import hmac
import os
import pwd
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

from atlasrag.server.config import ServerConfig, validate_loopback_host
from atlasrag.server.errors import ServerHttpError, TransportErrorCode

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
    """Read a regular, 0o600, current-UID file without following symlinks."""

    dir_info = path.parent.lstat()
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise PermissionError("state parent directory must not be a symlink")
    if dir_info.st_uid != os.getuid() or (stat.S_IMODE(dir_info.st_mode) & 0o077):
        raise PermissionError("state parent directory permissions or ownership unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("state file exceeds size bound")
        return data.decode("utf-8")
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
