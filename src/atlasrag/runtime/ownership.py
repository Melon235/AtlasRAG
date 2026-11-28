"""Machine-local deployment ownership using Linux advisory file locks."""

from __future__ import annotations

import fcntl
import os
import pwd
import stat
from enum import StrEnum
from pathlib import Path

from atlasrag.domain.errors import RuntimeOwnershipConflictError


class LockOwnershipState(StrEnum):
    """Observed Linux kernel FLOCK state for this OS process."""

    HELD = "HELD"
    NOT_HELD = "NOT_HELD"
    UNKNOWN = "UNKNOWN"


class OwnershipReleaseOutcome(StrEnum):
    """Explicit outcome recorded by RuntimeOwnershipLock.release()."""

    NOT_RELEASED = "NOT_RELEASED"
    CONFIRMED_RELEASED = "CONFIRMED_RELEASED"
    FAILED_LOCK_RETAINED = "FAILED_LOCK_RETAINED"
    FAILED_AFTER_RELEASE = "FAILED_AFTER_RELEASE"
    FAILED_UNKNOWN = "FAILED_UNKNOWN"


def inspect_process_flock_state(
    pid: int, lock_path: Path, *, has_open_fd: bool = False
) -> LockOwnershipState:
    """Inspect kernel /proc/<pid>/fdinfo rather than inferring from Python state flags."""

    if pid < 1:
        return LockOwnershipState.UNKNOWN
    try:
        dir_info = lock_path.parent.lstat()
    except FileNotFoundError:
        return (
            LockOwnershipState.UNKNOWN if has_open_fd else LockOwnershipState.NOT_HELD
        )
    except OSError:
        return LockOwnershipState.UNKNOWN

    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        return LockOwnershipState.UNKNOWN
    if dir_info.st_uid != os.getuid() or (stat.S_IMODE(dir_info.st_mode) & 0o077):
        return LockOwnershipState.UNKNOWN

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        probe_fd = os.open(lock_path, flags)
    except FileNotFoundError:
        return (
            LockOwnershipState.UNKNOWN if has_open_fd else LockOwnershipState.NOT_HELD
        )
    except OSError:
        return LockOwnershipState.UNKNOWN

    try:
        info = os.fstat(probe_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return LockOwnershipState.UNKNOWN
        if stat.S_IMODE(info.st_mode) & 0o077:
            return LockOwnershipState.UNKNOWN
        st_dev, st_ino = info.st_dev, info.st_ino
    finally:
        try:
            os.close(probe_fd)
        except OSError:
            pass

    expected_lock_id = f"{os.major(st_dev):02x}:{os.minor(st_dev):02x}:{st_ino}"
    fd_dir = Path(f"/proc/{pid}/fd")
    fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return LockOwnershipState.UNKNOWN

    for entry in entries:
        try:
            fd_stat = entry.stat()
        except FileNotFoundError:
            # Transient fd (such as the directory-listing fd) closed mid-iteration.
            continue
        except OSError:
            return LockOwnershipState.UNKNOWN
        if (fd_stat.st_dev, fd_stat.st_ino) != (st_dev, st_ino):
            continue
        try:
            info_text = (fdinfo_dir / entry.name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return LockOwnershipState.UNKNOWN
        for line in info_text.splitlines():
            if not line.startswith("lock:"):
                continue
            tokens = line.split()
            if (
                len(tokens) >= 7
                and tokens[2] == "FLOCK"
                and tokens[3] == "ADVISORY"
                and tokens[4] == "WRITE"
                and tokens[5].isdigit()
                and int(tokens[5]) == pid
                and tokens[6] == expected_lock_id
            ):
                return LockOwnershipState.HELD
    return LockOwnershipState.NOT_HELD


def canonical_lock_path() -> Path:
    """Return the fixed per-OS-user lock path for the supported deployment."""

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "atlasrag" / "runtime.lock"


class RuntimeOwnershipLock:
    """One exclusive lock per OS user; held for the runtime lifetime."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = canonical_lock_path() if path is None else path
        self._fd: int | None = None
        self._release_outcome = OwnershipReleaseOutcome.NOT_RELEASED

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    @property
    def release_outcome(self) -> OwnershipReleaseOutcome:
        return self._release_outcome

    def inspect_kernel_ownership(self) -> LockOwnershipState:
        """Inspect actual OS kernel FLOCK ownership for this process."""

        return inspect_process_flock_state(
            os.getpid(), self._path, has_open_fd=self._fd is not None
        )

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
        self._release_outcome = OwnershipReleaseOutcome.NOT_RELEASED

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        unlock_error: BaseException | None = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException as error:
            unlock_error = error

        close_error: OSError | None = None
        try:
            os.close(fd)
        except OSError as error:
            # On Linux, close() frees the descriptor table entry even on EIO/EINTR.
            # Never retry os.close(fd) against a potentially reused fd number.
            self._fd = None
            close_error = error
        else:
            self._fd = None

        actual = self.inspect_kernel_ownership()
        if unlock_error is not None or close_error is not None:
            if actual is LockOwnershipState.HELD:
                self._release_outcome = OwnershipReleaseOutcome.FAILED_LOCK_RETAINED
            elif actual is LockOwnershipState.NOT_HELD:
                self._release_outcome = OwnershipReleaseOutcome.FAILED_AFTER_RELEASE
            else:
                self._release_outcome = OwnershipReleaseOutcome.FAILED_UNKNOWN
            if unlock_error is not None:
                if close_error is not None:
                    raise unlock_error from close_error
                raise unlock_error
            assert close_error is not None
            raise close_error

        if actual is not LockOwnershipState.NOT_HELD:
            self._release_outcome = (
                OwnershipReleaseOutcome.FAILED_LOCK_RETAINED
                if actual is LockOwnershipState.HELD
                else OwnershipReleaseOutcome.FAILED_UNKNOWN
            )
            raise OSError(f"ownership release verification failed ({actual.value})")

        self._release_outcome = OwnershipReleaseOutcome.CONFIRMED_RELEASED
