from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
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
    WRITE_BLOCK_SIZE,
    WriteError,
    WriteProgressCallback,
    WriteSpec,
    write_image_to_block_io,
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


def _make_kernel32() -> ctypes.WinDLL:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lpvoid = ctypes.c_void_p
    lpdword = ctypes.POINTER(wintypes.DWORD)

    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        lpvoid,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE

    k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        lpvoid,
        wintypes.DWORD,
        lpvoid,
        wintypes.DWORD,
        lpdword,
        lpvoid,
    ]
    k32.DeviceIoControl.restype = wintypes.BOOL

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    k32.FindFirstVolumeW.restype = wintypes.HANDLE

    k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    k32.FindNextVolumeW.restype = wintypes.BOOL

    k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
    k32.FindVolumeClose.restype = wintypes.BOOL

    k32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    k32.SetFilePointerEx.restype = wintypes.BOOL

    k32.WriteFile.argtypes = [wintypes.HANDLE, lpvoid, wintypes.DWORD, lpdword, lpvoid]
    k32.WriteFile.restype = wintypes.BOOL

    k32.ReadFile.argtypes = [wintypes.HANDLE, lpvoid, wintypes.DWORD, lpdword, lpvoid]
    k32.ReadFile.restype = wintypes.BOOL

    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.FlushFileBuffers.restype = wintypes.BOOL
    return k32


class _Win32BlockIO:
    def __init__(self, kernel32: ctypes.WinDLL, h_disk: int, size: int) -> None:
        self._kernel32 = kernel32
        self._h_disk = h_disk
        self._size = size
        self._buf = mmap.mmap(-1, WRITE_BLOCK_SIZE)
        self._buf_addr = ctypes.addressof(ctypes.c_char.from_buffer(self._buf))

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self._buf.close()

    def _seek(self, offset: int) -> None:
        new_pos = ctypes.c_longlong(0)
        if not self._kernel32.SetFilePointerEx(self._h_disk, ctypes.c_longlong(offset), ctypes.byref(new_pos), 0):
            raise WriteError(f"Failed to seek target disk to offset {offset} (error {ctypes.get_last_error()}).")

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        if offset % SECTOR_SIZE != 0:
            raise WriteError(f"Unaligned disk write offset {offset}.")
        view = memoryview(data)
        pos = 0
        total = len(view)
        while pos < total:
            raw_len = min(WRITE_BLOCK_SIZE, total - pos)
            pad_len = (SECTOR_SIZE - (raw_len % SECTOR_SIZE)) % SECTOR_SIZE
            io_len = raw_len + pad_len
            self._buf[:raw_len] = view[pos : pos + raw_len]
            if pad_len:
                self._buf[raw_len:io_len] = b"\x00" * pad_len

            self._seek(offset + pos)
            bytes_written = wintypes.DWORD(0)
            if not self._kernel32.WriteFile(
                self._h_disk,
                ctypes.c_void_p(self._buf_addr),
                io_len,
                ctypes.byref(bytes_written),
                None,
            ) or int(bytes_written.value) != io_len:
                raise WriteError(f"Disk write failed (error {ctypes.get_last_error()}).")
            pos += raw_len

    def read_at(self, offset: int, size: int) -> bytes:
        aligned_off = (offset // SECTOR_SIZE) * SECTOR_SIZE
        lead_pad = offset - aligned_off
        total_need = lead_pad + size
        out = bytearray()
        pos = 0
        while pos < total_need:
            want = min(WRITE_BLOCK_SIZE, total_need - pos)
            io_want = want + ((SECTOR_SIZE - (want % SECTOR_SIZE)) % SECTOR_SIZE)
            self._seek(aligned_off + pos)
            bytes_read = wintypes.DWORD(0)
            if not self._kernel32.ReadFile(
                self._h_disk,
                ctypes.c_void_p(self._buf_addr),
                io_want,
                ctypes.byref(bytes_read),
                None,
            ) or int(bytes_read.value) < want:
                raise WriteError(f"Disk read failed (error {ctypes.get_last_error()}).")
            out.extend(memoryview(self._buf)[:want])
            pos += want
        return bytes(memoryview(out)[lead_pad : lead_pad + size])

    def flush(self) -> None:
        self._kernel32.FlushFileBuffers(self._h_disk)


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

    kernel32 = _make_kernel32()
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
            raise WriteError(f"Failed to open {spec.device} (error {ctypes.get_last_error()}).")

        ret = wintypes.DWORD(0)
        kernel32.DeviceIoControl(h_disk, FSCTL_ALLOW_EXTENDED_DASD_IO, None, 0, None, 0, ctypes.byref(ret), None)
        if spec.install_mode != "to_go_update":
            kernel32.DeviceIoControl(h_disk, IOCTL_DISK_DELETE_DRIVE_LAYOUT, None, 0, None, 0, ctypes.byref(ret), None)

        win_io = _Win32BlockIO(kernel32, h_disk, spec.expected_size)
        try:
            write_image_to_block_io(
                archive_path=spec.archive_path,
                member_name=spec.member_name,
                image_size=spec.image_size,
                expected_sha1=spec.expected_sha1,
                io=win_io,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
                before_verify_hook=None,
                verify_drive=spec.verify_drive,
                install_mode=spec.install_mode,
                expected_size=spec.expected_size,
                install_flexshim=spec.install_flexshim,
                cros_debug=spec.cros_debug,
                disable_updates=spec.disable_updates,
            )
            kernel32.DeviceIoControl(h_disk, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0, ctypes.byref(ret), None)
        finally:
            win_io.close()
    finally:
        if h_disk != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(h_disk)
        for lh in locked_vols:
            kernel32.CloseHandle(lh)
