"""Initialization and health of BIOINFO_HOME (the managed data directory).

The single most dangerous failure mode in this system is an unmounted external
drive. On macOS, Docker Desktop presents an unmounted bind-mount source as an
*empty directory*, not an error -- so a naive file-verification pass would
happily conclude that every file in the library has been deleted.

The guard is a sentinel file written at initialization. Its absence means "the
drive is gone", never "the files were deleted". Every verification batch checks
it before touching anything.
"""

import errno
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.errors import StorageUnavailableError
from app.logging import get_logger

log = get_logger(__name__)

SENTINEL_CONTENT = "biopipe-home-v1\n"

_lock_fd: int | None = None


@dataclass
class HomeStatus:
    ok: bool
    detail: str
    path: str


def initialize_home() -> None:
    """Create and validate the directory layout. Raises StorageUnavailableError.

    Called once at API startup, before serving traffic.
    """
    home = settings.bioinfo_home

    # The *parent* must exist. If it doesn't, the mount itself is missing --
    # creating it would silently write into the container's own filesystem,
    # which is precisely the disaster we're guarding against.
    if not home.parent.exists():
        raise StorageUnavailableError(
            f"The parent of BIOINFO_HOME does not exist: {home.parent}. "
            "On macOS this usually means the external drive is not mounted, or "
            "the path has not been added to Docker Desktop > Settings > "
            "Resources > File Sharing.",
            details={"home": str(home), "parent": str(home.parent)},
        )

    for d in (
        home,
        settings.objects_dir,
        settings.staging_dir,
        settings.tmp_dir,
        settings.logs_dir,
        settings.qc_reports_dir,
        settings.meta_dir,
    ):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageUnavailableError(
                f"Cannot create {d}: {e.strerror}",
                details={"path": str(d), "errno": e.errno},
            ) from e

    _assert_writable(home)
    _assert_same_filesystem()
    _write_sentinel()
    _acquire_lock()

    log.info("home_initialized", path=str(home))


def _assert_writable(home: Path) -> None:
    probe = settings.meta_dir / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise StorageUnavailableError(
            f"BIOINFO_HOME is not writable: {e.strerror}",
            details={"path": str(home), "errno": e.errno},
        ) from e


def _assert_same_filesystem() -> None:
    """objects/, staging/ and tmp/ must share a filesystem.

    Blob placement finishes with os.rename(), which is atomic only within a
    single filesystem. Across filesystems it raises EXDEV, which would surface
    as a confusing late failure at the end of a multi-GB upload rather than at
    startup.
    """
    dev_objects = settings.objects_dir.stat().st_dev
    for name, path in (("staging", settings.staging_dir), ("tmp", settings.tmp_dir)):
        if path.stat().st_dev != dev_objects:
            raise StorageUnavailableError(
                f"{name}/ and objects/ are on different filesystems, so atomic "
                "rename is impossible. Both must live under BIOINFO_HOME on one volume.",
                details={"objects": str(settings.objects_dir), name: str(path)},
            )


def _write_sentinel() -> None:
    if not settings.sentinel_path.exists():
        settings.sentinel_path.write_text(SENTINEL_CONTENT)


def _acquire_lock() -> None:
    """Refuse to run two stacks against one home directory.

    Two independent stacks sharing a home would race on blob refcounts and GC.
    The lock is advisory and released when the process exits.
    """
    global _lock_fd
    if _lock_fd is not None:
        return
    fd = os.open(settings.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EACCES, errno.EAGAIN):
            # Advisory locks are unreliable over some FUSE configurations, so a
            # failure here is a warning rather than a hard stop.
            log.warning("home_lock_contended", path=str(settings.lock_path))
            return
        log.warning("home_lock_unavailable", error=str(e))
        return
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd


def check_home() -> HomeStatus:
    """Cheap liveness check for /readyz and for every verification batch.

    Must not create anything -- if the drive is gone, mkdir would mask it.
    """
    home = settings.bioinfo_home
    if not home.exists():
        return HomeStatus(False, f"BIOINFO_HOME does not exist: {home}", str(home))
    if not settings.sentinel_path.exists():
        return HomeStatus(
            False,
            "Mount sentinel .biopipe/VERSION is missing. The drive is most likely "
            "unmounted. Refusing to treat stored files as deleted.",
            str(home),
        )
    if not os.access(home, os.W_OK):
        return HomeStatus(False, f"BIOINFO_HOME is not writable: {home}", str(home))
    return HomeStatus(True, "ok", str(home))


def require_home() -> None:
    """Raise if storage is unavailable. Use at the top of write paths."""
    status = check_home()
    if not status.ok:
        raise StorageUnavailableError(status.detail, details={"path": status.path})
