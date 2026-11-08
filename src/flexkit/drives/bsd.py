from __future__ import annotations

import logging
import re
import subprocess

from flexkit.drives import Drive, Volume, format_drive_name

log = logging.getLogger("flexkit.drives.bsd")

_GEOM_HEADER_RE = re.compile(r"^Geom name:\s*(\S+)")
_MEDIASIZE_RE = re.compile(r"^\s*Mediasize:\s*(\d+)")
_DESCR_RE = re.compile(r"^\s*descr:\s*(.+)$")
_IDENT_RE = re.compile(r"^\s*ident:\s*(.+)$")
_ROTRATE_RE = re.compile(r"^\s*rotationrate:\s*(\S+)")
_ACCESS_RE = re.compile(r"^\s*access:\s*(.+)$")
_CAM_LINE_RE = re.compile(r"<([^>]*)>\s+at\s+scbus\d+.*\(([^)]+)\)")


def parse_freebsd_geom(
    geom_text: str,
    mount_text: str = "",
    cam_text: str = "",
) -> list[Drive]:
    usb_buses: set[str] = set()
    for line in cam_text.splitlines():
        m = _CAM_LINE_RE.search(line)
        if m and "umass" in line.lower():
            for token in m.group(2).split(","):
                tok = token.strip()
                if tok.startswith("da"):
                    usb_buses.add(tok)

    mounts_by_base: dict[str, list[Volume]] = {}
    sys_bases: set[str] = set()
    for line in mount_text.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/"):
            continue
        dev, mp, fstype = parts[0], parts[1], parts[2]
        base_m = re.match(r"^/dev/([a-z]+\d+)", dev)
        if not base_m:
            continue
        base = base_m.group(1)
        mounts_by_base.setdefault(base, []).append(
            Volume(device=dev, label="", mountpoint=mp, fstype=fstype)
        )
        if mp in ("/", "/boot", "/usr", "/var", "/home"):
            sys_bases.add(base)

    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in geom_text.splitlines():
        hm = _GEOM_HEADER_RE.match(line)
        if hm:
            current = {"name": hm.group(1)}
            blocks.append(current)
            continue
        if current is None:
            continue
        for key, pat in (
            ("size", _MEDIASIZE_RE),
            ("descr", _DESCR_RE),
            ("ident", _IDENT_RE),
            ("rotrate", _ROTRATE_RE),
            ("access", _ACCESS_RE),
        ):
            m = pat.match(line)
            if m:
                current[key] = m.group(1).strip()

    drives: list[Drive] = []
    for blk in blocks:
        name = blk.get("name", "")
        if not name or name.startswith(("cd", "acd", "md")):
            continue
        size = int(blk.get("size", "0") or 0)
        descr = blk.get("descr", "")
        if descr == "(null)":
            descr = ""
        ident = blk.get("ident", "")
        if ident == "(null)":
            ident = ""

        if name.startswith("mmcsd"):
            bus = "SD"
            removable = True
        elif name in usb_buses or name.startswith("da"):
            bus = "USB"
            rotrate = blk.get("rotrate", "unknown")
            removable = rotrate in ("0", "unknown") and size <= 256_000_000_000
        elif name.startswith(("nvd", "nda")):
            bus = "NVMe"
            removable = False
        elif name.startswith("ada"):
            bus = "SATA"
            removable = False
        else:
            bus = "Other"
            removable = False

        drives.append(
            Drive(
                id=f"/dev/{name}",
                name=format_drive_name(None, descr, fallback=name),
                size=size,
                bus=bus,
                removable=removable,
                serial=ident,
                volumes=tuple(mounts_by_base.get(name, ())),
                system=(name in sys_bases),
                read_only=False,
            )
        )
    return drives


def list_physical_drives() -> list[Drive]:
    try:
        geom_out = subprocess.check_output(
            ["geom", "disk", "list"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        log.warning("geom disk list failed: %s", exc)
        return []

    mount_out = ""
    cam_out = ""
    for cmd, attr in ((["mount", "-p"], "mount"), (["camcontrol", "devlist"], "cam")):
        try:
            res = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=4.0)
            if attr == "mount":
                mount_out = res
            else:
                cam_out = res
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    return parse_freebsd_geom(geom_out, mount_out, cam_out)
