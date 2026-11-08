from __future__ import annotations

import logging
import plistlib
import re
import subprocess
from typing import Any

from flexkit.drives import Drive, Volume, format_drive_name

log = logging.getLogger("flexkit.drives.darwin")

_WHOLE_DISK_RE = re.compile(r"^(disk\d+)")


def _base_disk_id(bsd_name: str) -> str:
    m = _WHOLE_DISK_RE.match(bsd_name.removeprefix("/dev/"))
    return m.group(1) if m else bsd_name


def _map_bus(protocol: str) -> str:
    p = protocol.strip().upper()
    if p == "USB":
        return "USB"
    if p in ("SECURE DIGITAL", "SD", "MMC"):
        return "SD"
    if p in ("SATA", "SAS"):
        return "SATA"
    if p in ("PCI-EXPRESS", "NVME", "APPLE FABRIC"):
        return "NVMe"
    if p == "FIREWIRE":
        return "FireWire"
    return "Other"


def parse_darwin_plists(
    list_plist: dict[str, Any],
    info_by_id: dict[str, dict[str, Any]],
) -> list[Drive]:
    system_whole_disks: set[str] = set()
    apfs_store_map: dict[str, list[str]] = {}

    for disk_entry in list_plist.get("AllDisksAndPartitions") or []:
        if not isinstance(disk_entry, dict):
            continue
        stores = disk_entry.get("APFSPhysicalStores") or []
        container_id = str(disk_entry.get("DeviceIdentifier") or "")
        mapped = [_base_disk_id(str(s.get("DeviceIdentifier") or "")) for s in stores if isinstance(s, dict)]
        if container_id and mapped:
            apfs_store_map[container_id] = mapped
        for apfs_vol in disk_entry.get("APFSVolumes") or []:
            if not isinstance(apfs_vol, dict):
                continue
            mp = str(apfs_vol.get("MountPoint") or "")
            if mp == "/" or mp.startswith("/System/Volumes/"):
                for phys in mapped:
                    system_whole_disks.add(phys)
                system_whole_disks.add(_base_disk_id(container_id))

    drives: list[Drive] = []
    for disk_entry in list_plist.get("AllDisksAndPartitions") or []:
        if not isinstance(disk_entry, dict):
            continue
        disk_id = str(disk_entry.get("DeviceIdentifier") or "")
        if not disk_id or disk_id in apfs_store_map:
            continue
        info = info_by_id.get(disk_id)
        if not info or info.get("VirtualOrPhysical") == "Virtual":
            continue
        if bool(info.get("Internal")) and str(info.get("BusProtocol") or "").upper() not in ("USB", "SECURE DIGITAL"):
            system_whole_disks.add(disk_id)

        volumes: list[Volume] = []
        is_sys = disk_id in system_whole_disks
        top_mp = str(disk_entry.get("MountPoint") or info.get("MountPoint") or "")
        if top_mp == "/" or top_mp.startswith("/System"):
            is_sys = True

        for part in disk_entry.get("Partitions") or []:
            if not isinstance(part, dict):
                continue
            part_id = str(part.get("DeviceIdentifier") or "")
            pinfo = info_by_id.get(part_id, {})
            mp = str(part.get("MountPoint") or pinfo.get("MountPoint") or "")
            if mp == "/" or mp.startswith("/System"):
                is_sys = True
            label = str(part.get("VolumeName") or pinfo.get("VolumeName") or "")
            fstype = str(part.get("Content") or pinfo.get("FilesystemName") or "")
            if mp or label:
                volumes.append(Volume(device=f"/dev/{part_id}", label=label, mountpoint=mp, fstype=fstype))

        bus = _map_bus(str(info.get("BusProtocol") or ""))
        removable = bool(info.get("RemovableMedia") or info.get("Removable")) or bus == "SD"
        ro = not bool(info.get("Writable", True))
        size = int(info.get("TotalSize") or info.get("Size") or disk_entry.get("Size") or 0)
        vendor = str(info.get("MediaName") or "")
        model = str(info.get("IORegistryEntryName") or "")
        name = format_drive_name(None, vendor or model, fallback=disk_id)

        drives.append(
            Drive(
                id=f"/dev/{disk_id}",
                name=name,
                size=size,
                bus=bus,
                removable=removable,
                serial=str(info.get("DeviceTreePath") or ""),
                volumes=tuple(volumes),
                system=is_sys,
                read_only=ro,
            )
        )
    return drives


def list_physical_drives() -> list[Drive]:
    try:
        raw_all = subprocess.check_output(
            ["diskutil", "list", "-plist"],
            stderr=subprocess.DEVNULL,
            timeout=8.0,
        )
        list_data = plistlib.loads(raw_all)
    except (FileNotFoundError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        log.warning("diskutil list failed: %s", exc)
        return []

    info_by_id: dict[str, dict[str, Any]] = {}
    for entry in list_data.get("AllDisksAndPartitions") or []:
        if not isinstance(entry, dict):
            continue
        did = str(entry.get("DeviceIdentifier") or "")
        if not did:
            continue
        try:
            raw_info = subprocess.check_output(
                ["diskutil", "info", "-plist", did],
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            info_by_id[did] = plistlib.loads(raw_info)
        except (subprocess.SubprocessError, plistlib.InvalidFileException):
            continue

    return parse_darwin_plists(list_data, info_by_id)
