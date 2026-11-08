from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

from flexkit.catalog import format_bytes


@dataclass(frozen=True, slots=True)
class Volume:
    device: str
    label: str
    mountpoint: str
    fstype: str


@dataclass(frozen=True, slots=True)
class Drive:
    id: str
    name: str
    size: int
    bus: str
    removable: bool
    serial: str
    volumes: tuple[Volume, ...]
    system: bool
    read_only: bool
    has_chromeos: bool = False

    @property
    def is_usb_hard_drive(self) -> bool:
        return self.bus == "USB" and not self.removable

    @property
    def summary_line(self) -> str:
        parts = [format_bytes(self.size), self.bus, self.id]
        labels = [v.label or v.mountpoint for v in self.volumes if v.label or v.mountpoint]
        if labels:
            parts.append(", ".join(dict.fromkeys(labels)))
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class DriveEntry:
    drive: Drive
    disabled_reason: str | None = None

    @property
    def selectable(self) -> bool:
        return self.disabled_reason is None


_GENERIC_VENDORS = frozenset({"", "ata", "generic", "generic-", "usb", "unknown", "standard disk drives"})


def format_drive_name(vendor: str | None, model: str | None, fallback: str = "USB Drive") -> str:
    v = " ".join((vendor or "").split())
    m = " ".join((model or "").split())
    if v.lower() in _GENERIC_VENDORS or v.lower().startswith("0x"):
        v = ""
    if v and m:
        if m.lower().startswith(v.lower()):
            return m
        return f"{v} {m}"
    return m or v or fallback


def _path_is_within(target: Path, mountpoint: str) -> bool:
    if not mountpoint or mountpoint.startswith("["):
        return False
    try:
        mp = Path(mountpoint).resolve()
        return target == mp or mp in target.parents
    except OSError:
        return False


def _drive_hosts_path(drive: Drive, image_path: Path | None) -> bool:
    if image_path is None:
        return False
    probe = image_path
    try:
        probe = (image_path if image_path.exists() else image_path.parent).resolve()
    except OSError:
        pass

    target_dev: int | None = None
    try:
        if probe.exists():
            target_dev = os.stat(probe).st_dev
    except OSError:
        target_dev = None

    for vol in drive.volumes:
        if not vol.mountpoint or vol.mountpoint.startswith("["):
            continue
        if _path_is_within(probe, vol.mountpoint):
            return True
        if target_dev is not None:
            try:
                if os.path.exists(vol.mountpoint) and os.stat(vol.mountpoint).st_dev == target_dev:
                    return True
            except OSError:
                pass
    return False


def filter_drives(
    drives: list[Drive],
    *,
    include_usb_hdd: bool = False,
    image_path: Path | None = None,
    min_size: int = 0,
    is_local_image: bool = False,
) -> list[DriveEntry]:
    results: list[DriveEntry] = []
    for drv in drives:
        if drv.system or drv.read_only or drv.size <= 0:
            continue
        if drv.bus not in ("USB", "SD"):
            continue
        if not drv.removable and not include_usb_hdd:
            continue

        reason: str | None = None
        if _drive_hosts_path(drv, image_path):
            reason = "Contains the selected image" if is_local_image else "Contains the downloaded image"
        elif min_size > 0 and drv.size < min_size:
            reason = f"Too small ({format_bytes(min_size)} required)"

        results.append(DriveEntry(drive=drv, disabled_reason=reason))
    return results


def enumerate_all_drives() -> list[Drive]:
    if sys.platform.startswith("linux"):
        from flexkit.drives.linux import list_physical_drives

        return list_physical_drives()
    if sys.platform == "win32":
        from flexkit.drives.windows import list_physical_drives

        return list_physical_drives()
    if sys.platform == "darwin":
        from flexkit.drives.darwin import list_physical_drives

        return list_physical_drives()
    if "bsd" in sys.platform:
        from flexkit.drives.bsd import list_physical_drives

        return list_physical_drives()
    return []


def list_drives(
    *,
    include_usb_hdd: bool = False,
    image_path: Path | None = None,
    min_size: int = 0,
    is_local_image: bool = False,
) -> list[DriveEntry]:
    return filter_drives(
        enumerate_all_drives(),
        include_usb_hdd=include_usb_hdd,
        image_path=image_path,
        min_size=min_size,
        is_local_image=is_local_image,
    )
