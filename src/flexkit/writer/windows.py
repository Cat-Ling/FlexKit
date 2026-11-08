from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import hashlib
import mmap
import re
import sys
import time

from flexkit.drives.windows import (
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    INVALID_HANDLE_VALUE,
    IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
    OPEN_EXISTING,
    _VOLUME_DISK_EXTENTS,
)
from flexkit.writer import (
    CHROMEOS_OS_PAYLOAD_SIZE,
    WRITE_BLOCK_SIZE,
    WriteCancelled,
    WriteError,
    WriteProgressCallback,
    WriteSpec,
    open_image_source,
)

_PHYS_DRIVE_RE = re.compile(r"^\\\\\.\\PhysicalDrive([1-9]\d*)$")

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000

FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083
IOCTL_DISK_DELETE_DRIVE_LAYOUT = 0x0007C100
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
SECTOR_SIZE = 512


def _lock_and_dismount_volumes(kernel32: ctypes.WinDLL, disk_num: int) -> list[int]:
    locked_handles: list[int] = []
    vol_buf = ctypes.create_unicode_buffer(512)
    find_handle = kernel32.FindFirstVolumeW(vol_buf, len(vol_buf))
    if find_handle == INVALID_HANDLE_VALUE:
        return locked_handles

    try:
        while True:
            vol_path = vol_buf.value.rstrip("\\")
            h_probe = kernel32.CreateFileW(
                vol_path,
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            matches_disk = False
            if h_probe != INVALID_HANDLE_VALUE:
                try:
                    ext = _VOLUME_DISK_EXTENTS()
                    ret = wintypes.DWORD(0)
                    if kernel32.DeviceIoControl(
                        h_probe,
                        IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                        None,
                        0,
                        ctypes.byref(ext),
                        ctypes.sizeof(ext),
                        ctypes.byref(ret),
                        None,
                    ):
                        count = min(int(ext.NumberOfDiskExtents), 8)
                        matches_disk = any(int(ext.Extents[i].DiskNumber) == disk_num for i in range(count))
                finally:
                    kernel32.CloseHandle(h_probe)

            if matches_disk:
                h_vol = kernel32.CreateFileW(
                    vol_path,
                    GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None,
                    OPEN_EXISTING,
                    0,
                    None,
                )
                if h_vol == INVALID_HANDLE_VALUE:
                    for lh in locked_handles:
                        kernel32.CloseHandle(lh)
                    raise WriteError("Could not open volume on target drive for locking.")

                ret = wintypes.DWORD(0)
                locked = False
                for _ in range(15):
                    if kernel32.DeviceIoControl(h_vol, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
                        locked = True
                        break
                    time.sleep(0.2)
                if not locked:
                    kernel32.CloseHandle(h_vol)
                    for lh in locked_handles:
                        kernel32.CloseHandle(lh)
                    raise WriteError("Drive is in use by another application.")

                kernel32.DeviceIoControl(h_vol, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
                locked_handles.append(h_vol)

            if not kernel32.FindNextVolumeW(find_handle, vol_buf, len(vol_buf)):
                break
    finally:
        kernel32.FindVolumeClose(find_handle)

    return locked_handles


def write_physical_drive(
    spec: WriteSpec,
    *,
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    if sys.platform != "win32":
        raise WriteError("Windows disk writer requires win32.")
    m = _PHYS_DRIVE_RE.match(spec.device)
    if not m:
        raise WriteError(f"Refusing invalid Windows target device: {spec.device!r}")
    disk_num = int(m.group(1))

    kernel32 = ctypes.windll.kernel32
    locked_vols = _lock_and_dismount_volumes(kernel32, disk_num)
    h_disk = INVALID_HANDLE_VALUE

    try:
        h_disk = kernel32.CreateFileW(
            spec.device,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH,
            None,
        )
        if h_disk == INVALID_HANDLE_VALUE:
            raise WriteError(f"Failed to open {spec.device} (error {kernel32.GetLastError()}).")

        ret = wintypes.DWORD(0)
        kernel32.DeviceIoControl(h_disk, FSCTL_ALLOW_EXTENDED_DASD_IO, None, 0, None, 0, ctypes.byref(ret), None)
        kernel32.DeviceIoControl(h_disk, IOCTL_DISK_DELETE_DRIVE_LAYOUT, None, 0, None, 0, ctypes.byref(ret), None)

        aligned_buf = mmap.mmap(-1, WRITE_BLOCK_SIZE)
        try:
            buf_addr = ctypes.addressof(ctypes.c_char.from_buffer(aligned_buf))
            is_togo = spec.install_mode in ("to_go_fresh", "to_go_update")
            target_write_size = CHROMEOS_OS_PAYLOAD_SIZE if is_togo else spec.image_size

            write_sha1 = hashlib.sha1()
            written = 0
            win_start = time.monotonic()
            win_bytes = 0
            bps = 0
            if progress_cb is not None:
                progress_cb("write", 0, target_write_size, 0)

            with open_image_source(spec.archive_path, spec.member_name, spec.image_size) as src:
                while written < target_write_size:
                    if cancel_check is not None and cancel_check():
                        raise WriteCancelled()
                    to_read = min(WRITE_BLOCK_SIZE, target_write_size - written)
                    chunk = src.read(to_read)
                    if not chunk:
                        break
                    write_sha1.update(chunk)
                    raw_len = len(chunk)
                    pad_len = (SECTOR_SIZE - (raw_len % SECTOR_SIZE)) % SECTOR_SIZE
                    io_len = raw_len + pad_len
                    aligned_buf[:raw_len] = chunk
                    if pad_len:
                        aligned_buf[raw_len:io_len] = b"\x00" * pad_len

                    bytes_written = wintypes.DWORD(0)
                    if not kernel32.WriteFile(
                        h_disk,
                        ctypes.c_void_p(buf_addr),
                        io_len,
                        ctypes.byref(bytes_written),
                        None,
                    ) or int(bytes_written.value) != io_len:
                        raise WriteError(f"Disk write failed (error {kernel32.GetLastError()}).")

                    written += raw_len
                    win_bytes += raw_len
                    now = time.monotonic()
                    elapsed = now - win_start
                    if elapsed >= 0.4:
                        inst = int(win_bytes / elapsed)
                        bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                        win_start = now
                        win_bytes = 0
                    if progress_cb is not None:
                        progress_cb("write", written, target_write_size, bps)

            kernel32.FlushFileBuffers(h_disk)
            computed_sha1 = write_sha1.hexdigest().lower()
            if written != target_write_size or (
                spec.expected_sha1 and computed_sha1 != spec.expected_sha1.lower() and spec.install_mode == "installer"
            ):
                raise WriteError("Image write stream verification failed.")

            if not spec.verify_drive:
                kernel32.DeviceIoControl(h_disk, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0, ctypes.byref(ret), None)
                return

            new_pos = ctypes.c_longlong(0)
            if not kernel32.SetFilePointerEx(h_disk, ctypes.c_longlong(0), ctypes.byref(new_pos), 0):
                raise WriteError("Failed to seek disk for verification.")

            verify_sha1 = hashlib.sha1()
            checked = 0
            win_start = time.monotonic()
            win_bytes = 0
            bps = 0
            if progress_cb is not None:
                progress_cb("verify", 0, target_write_size, 0)

            while checked < target_write_size:
                if cancel_check is not None and cancel_check():
                    raise WriteCancelled()
                want = min(WRITE_BLOCK_SIZE, target_write_size - checked)
                io_want = want + ((SECTOR_SIZE - (want % SECTOR_SIZE)) % SECTOR_SIZE)
                bytes_read = wintypes.DWORD(0)
                if not kernel32.ReadFile(
                    h_disk,
                    ctypes.c_void_p(buf_addr),
                    io_want,
                    ctypes.byref(bytes_read),
                    None,
                ) or int(bytes_read.value) < want:
                    raise WriteError(f"Disk verification read failed (error {kernel32.GetLastError()}).")

                verify_sha1.update(memoryview(aligned_buf)[:want])
                checked += want
                win_bytes += want
                now = time.monotonic()
                elapsed = now - win_start
                if elapsed >= 0.4:
                    inst = int(win_bytes / elapsed)
                    bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                    win_start = now
                    win_bytes = 0
                if progress_cb is not None:
                    progress_cb("verify", checked, target_write_size, bps)

            if verify_sha1.hexdigest().lower() != computed_sha1 and spec.install_mode == "installer":
                raise WriteError("Verification failed: data read back from drive did not match image SHA-1.")

            kernel32.DeviceIoControl(h_disk, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0, ctypes.byref(ret), None)
        finally:
            aligned_buf.close()
    finally:
        if h_disk != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(h_disk)
        for lh in locked_vols:
            kernel32.CloseHandle(lh)
