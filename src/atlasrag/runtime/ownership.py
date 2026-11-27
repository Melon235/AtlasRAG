"""Machine-local deployment ownership using Linux advisory file locks."""

from __future__ import annotations

import fcntl
import os
import pwd
import stat
from pathlib import Path

from atlasrag.domain.errors import RuntimeOwnershipConflictError


def canonical_lock_path() -> Path:
    """Return the fixed per-OS-user lock path for the supported deployment."""

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "atlasrag" / "runtime.lock"


class RuntimeOwnershipLock:
    """One exclusive lock per OS user; held for the runtime lifetime."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = canonical_lock_path() if path is None else path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeOwnershipConflictError
        parent = self._path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_stat = parent.stat()
        except OSError as error:
            raise RuntimeOwnershipConflictError from error
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise RuntimeOwnershipConflictError
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            raise RuntimeOwnershipConflictError
        try:
            fd = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError as error:
            raise RuntimeOwnershipConflictError from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeOwnershipConflictError
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeOwnershipConflictError
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            os.close(fd)
            if isinstance(error, RuntimeOwnershipConflictError):
                raise
            raise RuntimeOwnershipConflictError from error
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
