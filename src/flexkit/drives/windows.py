from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import sys
from typing import Any

from flexkit.drives import Drive, Volume, format_drive_name

log = logging.getLogger("flexkit.drives.windows")

OPEN_EXISTING = 3
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_IS_WRITABLE = 0x00070024
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
ERROR_WRITE_PROTECT = 19

_BUS_MAP = {
    7: "USB",
    12: "SD",
    13: "SD",
    11: "SATA",
    3: "SATA",
    8: "SATA",
    10: "SATA",
    17: "NVMe",
    6: "FireWire",
}


class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", wintypes.DWORD),
        ("QueryType", wintypes.DWORD),
        ("AdditionalParameters", ctypes.c_ubyte * 1),
    ]


class _STORAGE_DEVICE_DESCRIPTOR_HEADER(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Size", wintypes.DWORD),
        ("DeviceType", ctypes.c_ubyte),
        ("DeviceTypeModifier", ctypes.c_ubyte),
        ("RemovableMedia", ctypes.c_ubyte),
        ("CommandQueueing", ctypes.c_ubyte),
        ("VendorIdOffset", wintypes.DWORD),
        ("ProductIdOffset", wintypes.DWORD),
        ("ProductRevisionOffset", wintypes.DWORD),
        ("SerialNumberOffset", wintypes.DWORD),
        ("BusType", wintypes.DWORD),
        ("RawPropertiesLength", wintypes.DWORD),
    ]


class _DISK_EXTENT(ctypes.Structure):
    _fields_ = [
        ("DiskNumber", wintypes.DWORD),
        ("StartingOffset", ctypes.c_longlong),
        ("ExtentLength", ctypes.c_longlong),
    ]


class _VOLUME_DISK_EXTENTS(ctypes.Structure):
    _fields_ = [
        ("NumberOfDiskExtents", wintypes.DWORD),
        ("Extents", _DISK_EXTENT * 8),
    ]


def _cstr_at(buf: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(buf):
        return ""
    end = buf.find(b"\x00", offset)
    raw = buf[offset:] if end == -1 else buf[offset:end]
    return raw.decode("ascii", errors="ignore").strip()


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

    k32.GetVolumePathNamesForVolumeNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        lpdword,
    ]
    k32.GetVolumePathNamesForVolumeNameW.restype = wintypes.BOOL

    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        lpdword,
        lpdword,
        lpdword,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL
    return k32


def _collect_volume_map(kernel32: Any) -> tuple[dict[int, list[Volume]], set[int]]:
    by_disk: dict[int, list[Volume]] = {}
    system_disks: set[int] = set()
    sys_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()

    vol_buf = ctypes.create_unicode_buffer(512)
    find_handle = kernel32.FindFirstVolumeW(vol_buf, len(vol_buf))
    if find_handle == INVALID_HANDLE_VALUE:
        return by_disk, system_disks

    try:
        while True:
            vol_guid = vol_buf.value
            dev_path = vol_guid.rstrip("\\")
            h_vol = kernel32.CreateFileW(
                dev_path,
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            disk_nums: list[int] = []
            if h_vol != INVALID_HANDLE_VALUE:
                try:
                    ext = _VOLUME_DISK_EXTENTS()
                    returned = wintypes.DWORD(0)
                    ok = kernel32.DeviceIoControl(
                        h_vol,
                        IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                        None,
                        0,
                        ctypes.byref(ext),
                        ctypes.sizeof(ext),
                        ctypes.byref(returned),
                        None,
                    )
                    if ok:
                        count = min(int(ext.NumberOfDiskExtents), 8)
                        disk_nums = [int(ext.Extents[i].DiskNumber) for i in range(count)]
                finally:
                    kernel32.CloseHandle(h_vol)

            paths_buf = ctypes.create_unicode_buffer(1024)
            needed = wintypes.DWORD(0)
            mountpoint = ""
            if kernel32.GetVolumePathNamesForVolumeNameW(
                vol_guid, paths_buf, len(paths_buf), ctypes.byref(needed)
            ):
                raw_paths = ctypes.wstring_at(paths_buf, needed.value).split("\x00")
                non_empty = [p for p in raw_paths if p]
                if non_empty:
                    mountpoint = non_empty[0]

            label_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(261)
            label = ""
            fstype = ""
            if kernel32.GetVolumeInformationW(
                vol_guid,
                label_buf,
                len(label_buf),
                None,
                None,
                None,
                fs_buf,
                len(fs_buf),
            ):
                label = label_buf.value
                fstype = fs_buf.value

            dev_display = mountpoint.rstrip("\\") if mountpoint else dev_path
            vol = Volume(device=dev_display, label=label, mountpoint=mountpoint, fstype=fstype)
            is_sys = dev_display.upper() == sys_drive

            for dnum in disk_nums:
                by_disk.setdefault(dnum, []).append(vol)
                if is_sys:
                    system_disks.add(dnum)

            if not kernel32.FindNextVolumeW(find_handle, vol_buf, len(vol_buf)):
                break
    finally:
        kernel32.FindVolumeClose(find_handle)

    return by_disk, system_disks


def list_physical_drives() -> list[Drive]:
    if sys.platform != "win32":
        return []

    kernel32 = _make_kernel32()
    vol_map, sys_disks = _collect_volume_map(kernel32)
    drives: list[Drive] = []

    for idx in range(32):
        path = rf"\\.\PhysicalDrive{idx}"
        handle = kernel32.CreateFileW(
            path,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            continue

        try:
            query = _STORAGE_PROPERTY_QUERY(0, 0, (ctypes.c_ubyte * 1)(0))
            out_buf = ctypes.create_string_buffer(4096)
            returned = wintypes.DWORD(0)
            if not kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                out_buf,
                len(out_buf),
                ctypes.byref(returned),
                None,
            ):
                continue

            raw = out_buf.raw[: returned.value]
            if len(raw) < ctypes.sizeof(_STORAGE_DEVICE_DESCRIPTOR_HEADER):
                continue
            hdr = _STORAGE_DEVICE_DESCRIPTOR_HEADER.from_buffer_copy(raw)
            vendor = _cstr_at(raw, hdr.VendorIdOffset)
            model = _cstr_at(raw, hdr.ProductIdOffset)
            serial = _cstr_at(raw, hdr.SerialNumberOffset)
            bus = _BUS_MAP.get(int(hdr.BusType), "Other")
            removable = bool(hdr.RemovableMedia) or bus == "SD"

            size_val = ctypes.c_longlong(0)
            if not kernel32.DeviceIoControl(
                handle,
                IOCTL_DISK_GET_LENGTH_INFO,
                None,
                0,
                ctypes.byref(size_val),
                ctypes.sizeof(size_val),
                ctypes.byref(returned),
                None,
            ):
                continue

            read_only = False
            if not kernel32.DeviceIoControl(
                handle,
                IOCTL_DISK_IS_WRITABLE,
                None,
                0,
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                if ctypes.get_last_error() == ERROR_WRITE_PROTECT:
                    read_only = True

            drives.append(
                Drive(
                    id=path,
                    name=format_drive_name(vendor, model, fallback=f"Disk {idx}"),
                    size=int(size_val.value),
                    bus=bus,
                    removable=removable,
                    serial=serial,
                    volumes=tuple(vol_map.get(idx, ())),
                    system=(idx in sys_disks or idx == 0),
                    read_only=read_only,
                )
            )
        finally:
            kernel32.CloseHandle(handle)

    return drives
