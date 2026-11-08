from __future__ import annotations

from collections.abc import Callable
import fcntl
import os
import re
import subprocess
import sys

from flexkit.drives import enumerate_all_drives
from flexkit.writer import WriteError, WriteProgressCallback, WriteSpec, stream_zip_to_fd

_VALID_DEV_RE = re.compile(r"^/dev/(sd[a-z]+|mmcblk\d+|disk\d+|da\d+|mmcsd\d+)$")
_LINUX_BLKRRPART = 0x125F
_LINUX_BLKFLSBUF = 0x1261


def _unmount_target(device: str) -> None:
    if sys.platform == "darwin":
        res = subprocess.run(
            ["diskutil", "unmountDisk", "force", device],
            capture_output=True,
            text=True,
            timeout=20.0,
        )
        if res.returncode != 0:
            raise WriteError(f"Could not unmount {device}: {res.stderr.strip() or res.stdout.strip()}")
        return

    drives = [d for d in enumerate_all_drives() if d.id == device]
    if not drives:
        return
    seen_devs: set[str] = set()
    for vol in drives[0].volumes:
        if not vol.mountpoint or vol.device in seen_devs:
            continue
        seen_devs.add(vol.device)
        res = subprocess.run(
            ["umount", vol.device],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if res.returncode != 0:
            alt = subprocess.run(
                ["udisksctl", "unmount", "-b", vol.device, "--no-user-interaction"],
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            if alt.returncode != 0:
                raise WriteError(
                    f"Drive {vol.device} is busy ({vol.mountpoint}). Close open files and try again."
                )


def _raw_device_path(device: str) -> str:
    if sys.platform == "darwin" and device.startswith("/dev/disk"):
        return "/dev/rdisk" + device.removeprefix("/dev/disk")
    return device


def _flush_block_cache(fd: int, size: int) -> None:
    if sys.platform.startswith("linux"):
        try:
            fcntl.ioctl(fd, _LINUX_BLKFLSBUF, 0)
        except OSError:
            pass
        fadvise = getattr(os, "posix_fadvise", None)
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
        if fadvise is not None and dontneed is not None:
            try:
                fadvise(fd, 0, size, dontneed)
            except OSError:
                pass


def _finalize_device(device: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(
            ["diskutil", "eject", device],
            capture_output=True,
            timeout=15.0,
        )
        return
    if sys.platform.startswith("linux"):
        try:
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
            try:
                fcntl.ioctl(fd, _LINUX_BLKRRPART)
            finally:
                os.close(fd)
        except OSError:
            pass
        subprocess.run(["udevadm", "settle", "-t", "5"], capture_output=True, timeout=6.0)


def write_block_device(
    spec: WriteSpec,
    *,
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    if not _VALID_DEV_RE.match(spec.device):
        raise WriteError(f"Invalid target device path: {spec.device!r}")

    _unmount_target(spec.device)
    io_path = _raw_device_path(spec.device)

    flags = os.O_WRONLY
    if sys.platform.startswith("linux"):
        flags |= os.O_EXCL

    try:
        wfd = os.open(io_path, flags)
    except PermissionError as exc:
        raise WriteError("Permission denied opening target drive.") from exc
    except OSError as exc:
        raise WriteError(f"Could not gain exclusive access to {spec.device}: {exc}") from exc

    wfd_closed = False

    def close_writer_and_flush() -> None:
        nonlocal wfd_closed
        if not wfd_closed:
            _flush_block_cache(wfd, spec.image_size)
            os.close(wfd)
            wfd_closed = True

    def open_verify_fd() -> int:
        rfd = os.open(io_path, os.O_RDONLY)
        _flush_block_cache(rfd, spec.image_size)
        return rfd

    try:
        stream_zip_to_fd(
            archive_path=spec.archive_path,
            member_name=spec.member_name,
            image_size=spec.image_size,
            expected_sha1=spec.expected_sha1,
            write_fd=wfd,
            open_read_fd=open_verify_fd,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            before_verify_hook=close_writer_and_flush,
        )
    finally:
        if not wfd_closed:
            os.close(wfd)

    _finalize_device(spec.device)
