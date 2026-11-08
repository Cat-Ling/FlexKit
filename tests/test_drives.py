from __future__ import annotations

import json
from pathlib import Path

from flexkit.drives import filter_drives
from flexkit.drives.bsd import parse_freebsd_geom
from flexkit.drives.darwin import parse_darwin_plists
from flexkit.drives.linux import parse_lsblk_json

LSBLK_FIXTURE = {
    "blockdevices": [
        {
            "name": "nvme0n1",
            "path": "/dev/nvme0n1",
            "size": 512110190592,
            "vendor": None,
            "model": "Samsung SSD 980",
            "serial": "S649NX0R",
            "tran": "nvme",
            "rm": False,
            "hotplug": False,
            "ro": False,
            "type": "disk",
            "label": None,
            "fstype": None,
            "mountpoints": [],
            "children": [
                {
                    "name": "nvme0n1p2",
                    "path": "/dev/nvme0n1p2",
                    "size": 511000000000,
                    "type": "part",
                    "fstype": "crypto_LUKS",
                    "mountpoints": [],
                    "children": [
                        {
                            "name": "cryptroot",
                            "path": "/dev/mapper/cryptroot",
                            "type": "crypt",
                            "fstype": "LVM2_member",
                            "mountpoints": [],
                            "children": [
                                {
                                    "name": "vg0-root",
                                    "path": "/dev/mapper/vg0-root",
                                    "type": "lvm",
                                    "fstype": "ext4",
                                    "mountpoints": ["/"],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "name": "sdc",
            "path": "/dev/sdc",
            "size": 240057409536,
            "vendor": "CT240BX5",
            "model": "CT240BX500SSD1",
            "serial": "2411E89FDAAC",
            "tran": "usb",
            "rm": False,
            "hotplug": False,
            "ro": False,
            "type": "disk",
            "mountpoints": [],
            "children": [
                {"name": "sdc1", "path": "/dev/sdc1", "type": "part", "fstype": "vfat", "mountpoints": ["/boot/efi"]},
                {"name": "sdc2", "path": "/dev/sdc2", "type": "part", "fstype": "ext4", "mountpoints": ["/"]},
            ],
        },
        {
            "name": "sdd",
            "path": "/dev/sdd",
            "size": 256060514304,
            "vendor": "Realtek ",
            "model": "RTL9210CN NVME",
            "serial": "012345678938",
            "tran": "usb",
            "rm": False,
            "hotplug": False,
            "ro": False,
            "type": "disk",
            "mountpoints": [],
            "children": [
                {
                    "name": "sdd1",
                    "path": "/dev/sdd1",
                    "type": "part",
                    "label": "acer",
                    "fstype": "exfat",
                    "mountpoints": ["/mnt/external_ssd"],
                }
            ],
        },
        {
            "name": "sde",
            "path": "/dev/sde",
            "size": 31914983424,
            "vendor": "SanDisk",
            "model": "SanDisk Ultra",
            "serial": "4C53000123",
            "tran": "usb",
            "rm": True,
            "hotplug": True,
            "ro": False,
            "type": "disk",
            "mountpoints": [],
            "children": [
                {
                    "name": "sde1",
                    "path": "/dev/sde1",
                    "type": "part",
                    "label": "MYUSB",
                    "fstype": "vfat",
                    "mountpoints": ["/media/user/MYUSB"],
                }
            ],
        },
        {
            "name": "sdf",
            "path": "/dev/sdf",
            "size": 7800000000,
            "vendor": "Kingston",
            "model": "DataTraveler",
            "serial": "KDT8G",
            "tran": "usb",
            "rm": True,
            "hotplug": True,
            "ro": False,
            "type": "disk",
            "mountpoints": [],
        },
    ]
}


def test_linux_drive_safety_and_usb_hdd_toggle(tmp_path: Path) -> None:
    drives = parse_lsblk_json(json.dumps(LSBLK_FIXTURE))
    by_id = {d.id: d for d in drives}

    # Both LVM-on-LUKS NVMe and USB-connected OS root SSD must be flagged as system disks.
    assert by_id["/dev/nvme0n1"].system is True
    assert by_id["/dev/sdc"].system is True
    assert by_id["/dev/sde"].name == "SanDisk Ultra"

    min_img = 9523261952

    default_list = filter_drives(drives, include_usb_hdd=False, image_path=tmp_path / "flex.zip", min_size=min_img)
    assert [e.drive.id for e in default_list] == ["/dev/sde", "/dev/sdf"]
    assert default_list[0].selectable is True
    assert default_list[1].selectable is False
    assert default_list[1].disabled_reason == "Too small (9.52 GB required)"

    with_hdd = filter_drives(drives, include_usb_hdd=True, image_path=tmp_path / "flex.zip", min_size=min_img)
    assert [e.drive.id for e in with_hdd] == ["/dev/sdd", "/dev/sde", "/dev/sdf"]
    assert with_hdd[0].drive.is_usb_hard_drive is True
    assert with_hdd[0].selectable is True

    # Saving the image onto /mnt/external_ssd must disable /dev/sdd while keeping /dev/sde selectable.
    hosted_list = filter_drives(
        drives,
        include_usb_hdd=True,
        image_path=Path("/mnt/external_ssd/downloads/flex.zip"),
        min_size=min_img,
    )
    sdd_entry = next(e for e in hosted_list if e.drive.id == "/dev/sdd")
    assert sdd_entry.selectable is False
    assert sdd_entry.disabled_reason == "Contains the downloaded image"


def test_darwin_apfs_root_excluded() -> None:
    list_plist = {
        "AllDisksAndPartitions": [
            {"DeviceIdentifier": "disk0", "Size": 500107862016, "Partitions": [{"DeviceIdentifier": "disk0s2"}]},
            {
                "DeviceIdentifier": "disk3",
                "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
                "APFSVolumes": [{"DeviceIdentifier": "disk3s1", "MountPoint": "/"}],
            },
            {
                "DeviceIdentifier": "disk4",
                "Size": 32000000000,
                "Partitions": [{"DeviceIdentifier": "disk4s1", "VolumeName": "USBSTICK", "MountPoint": "/Volumes/USBSTICK"}],
            },
        ]
    }
    info_map = {
        "disk0": {"Internal": True, "BusProtocol": "Apple Fabric", "TotalSize": 500107862016, "Writable": True},
        "disk4": {
            "Internal": False,
            "BusProtocol": "USB",
            "RemovableMedia": True,
            "TotalSize": 32000000000,
            "MediaName": "Samsung BAR Plus",
            "Writable": True,
        },
    }
    drives = parse_darwin_plists(list_plist, info_map)
    filtered = filter_drives(drives, include_usb_hdd=False, min_size=9523261952)
    assert [e.drive.id for e in filtered] == ["/dev/disk4"]
    assert filtered[0].drive.name == "Samsung BAR Plus"


def test_freebsd_geom_parsing() -> None:
    geom_txt = """Geom name: ada0
Providers:
1. Name: ada0
   Mediasize: 256060514304 (238G)
   descr: KINGSTON SA400S37240G
   ident: 50026B7782
   rotationrate: 0

Geom name: da0
Providers:
1. Name: da0
   Mediasize: 31914983424 (30G)
   descr: SanDisk Ultra Fit
   ident: 4C53100999
   rotationrate: 0
"""
    mount_txt = "/dev/ada0p2 / ufs rw 1 1\n"
    cam_txt = "<SanDisk Ultra Fit 1.00> at scbus2 target 0 lun 0 (da0,pass2) umass-sim0\n"
    drives = parse_freebsd_geom(geom_txt, mount_txt, cam_txt)
    filtered = filter_drives(drives, include_usb_hdd=False, min_size=9523261952)
    assert [e.drive.id for e in filtered] == ["/dev/da0"]
    assert filtered[0].drive.serial == "4C53100999"
