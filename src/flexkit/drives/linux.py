from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from flexkit.drives import Drive, Volume, format_drive_name
from flexkit.writer.gpt import MIN_TOGO_ROOT_SECTORS, SECTOR_SIZE, is_chromeos_togo_drive

log = logging.getLogger("flexkit.drives.linux")

_SYSTEM_MOUNTS = frozenset(
    {
        "/",
        "/boot",
        "/boot/efi",
        "/efi",
        "/usr",
        "/var",
        "/var/tmp",
        "/home",
        "/root",
        "/etc",
        "/opt",
        "/nix",
        "[SWAP]",
    }
)


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return False


def _mountpoints_of(node: dict[str, Any]) -> list[str]:
    raw = node.get("mountpoints")
    if isinstance(raw, list):
        return [str(m) for m in raw if m]
    single = node.get("mountpoint")
    if single:
        return [str(single)]
    return []


def _collect_tree(
    node: dict[str, Any],
    volumes: list[Volume],
    part_sizes: dict[str, int],
    *,
    is_root_node: bool = False,
) -> tuple[bool, bool]:
    is_system = False
    any_ro = False
    mps = _mountpoints_of(node)
    fstype = str(node.get("fstype") or "")
    label = str(node.get("label") or "")
    partlabel = str(node.get("partlabel") or "").strip().upper()
    dev_path = str(node.get("path") or f"/dev/{node.get('name', '')}")

    if partlabel and str(node.get("type") or "") == "part":
        raw_sz = node.get("size")
        try:
            part_sizes[partlabel] = int(raw_sz) if raw_sz is not None else 0
        except (TypeError, ValueError):
            part_sizes[partlabel] = 0

    if fstype == "swap":
        is_system = True
    for mp in mps:
        if mp in _SYSTEM_MOUNTS or mp.startswith("/snap/"):
            is_system = True

    children = node.get("children")
    has_children = isinstance(children, list) and len(children) > 0

    if not is_root_node or (not has_children and (mps or label or fstype)):
        if mps:
            for mp in mps:
                if not mp.startswith("["):
                    volumes.append(Volume(device=dev_path, label=label, mountpoint=mp, fstype=fstype))
        elif label or fstype:
            volumes.append(Volume(device=dev_path, label=label, mountpoint="", fstype=fstype))

    if has_children:
        for child in children:
            if isinstance(child, dict):
                child_sys, child_ro = _collect_tree(child, volumes, part_sizes, is_root_node=False)
                is_system = is_system or child_sys
                any_ro = any_ro or child_ro

    return is_system, any_ro


def _classify_bus(tran: str, name: str) -> str:
    t = tran.lower()
    if t == "usb":
        return "USB"
    if t == "mmc" or name.startswith("mmcblk"):
        return "SD"
    if t in ("sata", "ata", "sas", "scsi"):
        return "SATA"
    if t == "nvme":
        return "NVMe"
    if t == "ieee1394":
        return "FireWire"
    return "Other"


def _has_togo_partitions(part_sizes: dict[str, int]) -> bool:
    required = {"STATE", "KERN-A", "KERN-B", "ROOT-A", "ROOT-B", "EFI-SYSTEM"}
    if not required.issubset(part_sizes.keys()):
        return False
    root_a_sz = part_sizes["ROOT-A"]
    root_b_sz = part_sizes["ROOT-B"]
    return root_a_sz == root_b_sz and root_a_sz >= MIN_TOGO_ROOT_SECTORS * SECTOR_SIZE


def parse_lsblk_json(payload: str | bytes) -> list[Drive]:
    data = json.loads(payload)
    devices = data.get("blockdevices") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        return []

    drives: list[Drive] = []
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        dev_type = str(dev.get("type") or "")
        name = str(dev.get("name") or "")
        if dev_type != "disk" or name.startswith(("loop", "ram", "zram", "fd", "sr", "dm-", "md")):
            continue

        dev_path = str(dev.get("path") or f"/dev/{name}")
        raw_size = dev.get("size")
        try:
            size = int(raw_size) if raw_size is not None else 0
        except (TypeError, ValueError):
            size = 0

        tran = str(dev.get("tran") or "").strip()
        bus = _classify_bus(tran, name)
        rm = _as_bool(dev.get("rm"))
        hotplug = _as_bool(dev.get("hotplug"))
        ro = _as_bool(dev.get("ro"))
        removable = (rm or hotplug) if bus == "SD" else rm

        volumes: list[Volume] = []
        part_sizes: dict[str, int] = {}
        is_system, _ = _collect_tree(dev, volumes, part_sizes, is_root_node=True)

        if part_sizes:
            has_cros = _has_togo_partitions(part_sizes)
        else:
            has_cros = is_chromeos_togo_drive(dev_path)

        drives.append(
            Drive(
                id=dev_path,
                name=format_drive_name(dev.get("vendor"), dev.get("model"), fallback=name),
                size=size,
                bus=bus,
                removable=removable,
                serial=str(dev.get("serial") or "").strip(),
                volumes=tuple(volumes),
                system=is_system,
                read_only=ro,
                has_chromeos=has_cros,
            )
        )
    return drives


def list_physical_drives() -> list[Drive]:
    base_cols = "NAME,PATH,SIZE,VENDOR,MODEL,SERIAL,TRAN,RM,HOTPLUG,RO,TYPE,LABEL,PARTLABEL,FSTYPE,MOUNTPOINTS"
    candidates = (
        base_cols,
        base_cols.replace("MOUNTPOINTS", "MOUNTPOINT"),
        base_cols.replace(",PARTLABEL", ""),
        base_cols.replace(",PARTLABEL", "").replace("MOUNTPOINTS", "MOUNTPOINT"),
    )
    for col_spec in candidates:
        try:
            out = subprocess.check_output(
                ["lsblk", "-J", "-b", "-o", col_spec],
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            return parse_lsblk_json(out)
        except subprocess.CalledProcessError:
            continue
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            log.warning("lsblk enumeration failed: %s", exc)
            return []
    return []
