from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import zipfile

import pytest

from flexkit.writer import (
    CHROMEOS_OS_PAYLOAD_SIZE,
    WriteCancelled,
    WriteError,
    WriteSpec,
    stream_zip_to_fd,
)
from flexkit.writer.gpt import (
    CHROMEOS_KERN_TYPE,
    CHROMEOS_ROOT_TYPE,
    CHROMEOS_STATE_TYPE,
    EFI_SYSTEM_TYPE,
    ROOT_A_UNIQUE_UUID,
    ROOT_B_UNIQUE_UUID,
    SECTOR_SIZE,
    ChromeOSBootOptions,
    build_protective_mbr,
    generate_chromeos_togo_partitions,
    parse_grub_cfg_options,
    patch_grub_cfg,
    patch_grub_in_block_device,
    read_chromeos_boot_options,
    serialize_gpt,
)

SAMPLE_GRUB_CFG = """defaultA=2
defaultB=3
gptpriority $grubdisk 2 prioA
gptpriority $grubdisk 4 prioB

if [ $prioA -lt $prioB ]; then
  set default=$defaultB
else
  set default=$defaultA
fi

set timeout=2

# NOTE: These magic grub variables are a Chrome OS hack. They are not portable.

menuentry "local image A" {
  linux /syslinux/vmlinuz.A quiet init=/sbin/init rootwait ro noresume loglevel=7 noinitrd console= kvm-intel.vmentry_l1d_flush=always  i915.modeset=1 cros_efi       root=PARTUUID=FDECD5E5-F2F2-CE47-852B-3839E867212B
}

menuentry "local image B" {
  linux /syslinux/vmlinuz.B quiet init=/sbin/init rootwait ro noresume loglevel=7 noinitrd console= kvm-intel.vmentry_l1d_flush=always  i915.modeset=1 cros_efi       root=PARTUUID=C99B551E-01AC-7345-864F-A9FDBD74F6C1
}

menuentry "verified image A" {
  linux /syslinux/vmlinuz.A quiet init=/sbin/init rootwait ro noresume loglevel=7 noinitrd console= kvm-intel.vmentry_l1d_flush=always  dm_verity.error_behavior=3 dm_verity.max_bios=-1 dm_verity.dev_wait=1       i915.modeset=1 cros_efi root=/dev/dm-0 dm="1 vroot none ro 1,0 4710400 verity payload=PARTUUID=FDECD5E5-F2F2-CE47-852B-3839E867212B hashtree=PARTUUID=FDECD5E5-F2F2-CE47-852B-3839E867212B hashstart=4710400 alg=sha256 root_hexdigest=9c383fc0b269639e90ce1bb533bebfe634a2ba9bf77927710f720c043b58f287 salt=de44ab57da563760ce136eefa58b93aa5c664c23ec4a675a29db1eed3cf9c0dd"
}

menuentry "verified image B" {
  linux /syslinux/vmlinuz.B quiet init=/sbin/init rootwait ro noresume loglevel=7 noinitrd console= kvm-intel.vmentry_l1d_flush=always  dm_verity.error_behavior=3 dm_verity.max_bios=-1 dm_verity.dev_wait=1       i915.modeset=1 cros_efi root=/dev/dm-0 dm="1 vroot none ro 1,0 4710400 verity payload=PARTUUID=C99B551E-01AC-7345-864F-A9FDBD74F6C1 hashtree=PARTUUID=C99B551E-01AC-7345-864F-A9FDBD74F6C1 hashstart=4710400 alg=sha256 root_hexdigest=9c383fc0b269639e90ce1bb533bebfe634a2ba9bf77927710f720c043b58f287 salt=de44ab57da563760ce136eefa58b93aa5c664c23ec4a675a29db1eed3cf9c0dd"
}

# FIXME: usb doesn't support verified boot for now
menuentry "Alternate USB Boot" {
  linux (hd0,3)/boot/vmlinuz quiet init=/sbin/init rootwait ro noresume loglevel=7 noinitrd console= kvm-intel.vmentry_l1d_flush=always  root=PARTUUID=FDECD5E5-F2F2-CE47-852B-3839E867212B i915.modeset=1 cros_efi
}
"""


def test_grub_cfg_options_patch_and_parse() -> None:
    opts = ChromeOSBootOptions(
        cros_debug=True,
        verbose_boot=True,
        timeout=5,
        nomodeset=True,
    )
    patched = patch_grub_cfg(SAMPLE_GRUB_CFG, opts)

    assert "set timeout=5" in patched
    assert "cros_debug" in patched
    assert "console=tty1" in patched
    assert "quiet" not in patched
    assert "nomodeset" in patched
    assert "i915.modeset=1" not in patched

    parsed = parse_grub_cfg_options(patched)
    assert parsed.cros_debug is True
    assert parsed.verbose_boot is True
    assert parsed.timeout == 5
    assert parsed.nomodeset is True


def test_generate_chromeos_togo_partitions_and_serialize_gpt() -> None:
    # Test a 16 GB drive (31250000 sectors)
    total_sectors = 31250000
    parts = generate_chromeos_togo_partitions(total_sectors)

    # Validate partition 1 (STATE) uses all remaining sectors
    state_part = next(p for p in parts if p[0] == 1)
    assert state_part[1] == "STATE"
    assert state_part[2] == CHROMEOS_STATE_TYPE
    assert state_part[4] == 6004736  # start LBA
    assert state_part[5] == total_sectors - 34  # expands to end of drive

    # Validate ROOT-A partition GUID matches dm-verity
    root_a = next(p for p in parts if p[0] == 3)
    assert root_a[3] == ROOT_A_UNIQUE_UUID

    # Serialize GPT
    mbr = build_protective_mbr(total_sectors)
    assert len(mbr) == 512
    assert mbr[510:512] == b"\x55\xaa"

    pri_hdr, entries, bak_hdr = serialize_gpt(total_sectors, parts)
    assert len(pri_hdr) == 512
    assert len(bak_hdr) == 512
    assert len(entries) == 128 * 128
    assert pri_hdr[:8] == b"EFI PART"
    assert bak_hdr[:8] == b"EFI PART"


def test_patch_grub_in_block_device_and_read(tmp_path: Path) -> None:
    img_path = tmp_path / "test_drive.img"
    total_sectors = 650000
    # Create empty image file with GPT and EFI-SYSTEM
    with open(img_path, "wb") as f:
        f.truncate(total_sectors * 512)

    parts = [
        (12, "EFI-SYSTEM", EFI_SYSTEM_TYPE, ROOT_A_UNIQUE_UUID, 299008, 600000, 4),
        (1, "STATE", CHROMEOS_STATE_TYPE, ROOT_B_UNIQUE_UUID, 600001, total_sectors - 34, 0),
        (3, "ROOT-A", CHROMEOS_ROOT_TYPE, ROOT_A_UNIQUE_UUID, 100, 299000, 0),
        (2, "KERN-A", CHROMEOS_KERN_TYPE, ROOT_B_UNIQUE_UUID, 64, 99, 0),
    ]
    mbr = build_protective_mbr(total_sectors)
    pri_hdr, entries, bak_hdr = serialize_gpt(total_sectors, parts)

    with open(img_path, "r+b") as f:
        f.seek(0)
        f.write(mbr)
        f.write(pri_hdr)
        f.write(entries)

        # Place FAT directory entry and sample grub.cfg in EFI-SYSTEM
        efi_base = 299008 * 512
        f.seek(efi_base + 634880)
        f.write(SAMPLE_GRUB_CFG.encode("utf-8"))

    # Verify options before patch
    opts_before = read_chromeos_boot_options(img_path)
    assert opts_before is not None
    assert opts_before.cros_debug is False
    assert opts_before.timeout == 2

    # Patch boot options in the disk image
    target_opts = ChromeOSBootOptions(cros_debug=True, verbose_boot=True, timeout=10, nomodeset=True)
    ok = patch_grub_in_block_device(img_path, target_opts)
    assert ok is True

    # Read back boot options from the disk image
    opts_after = read_chromeos_boot_options(img_path)
    assert opts_after is not None
    assert opts_after.cros_debug is True
    assert opts_after.verbose_boot is True
    assert opts_after.timeout == 10
    assert opts_after.nomodeset is True


def test_stream_zip_to_fd_skip_verify(tmp_path: Path) -> None:
    data = b"X" * (1024 * 1024)
    zip_path = tmp_path / "img.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("test.bin", data)

    dest_path = tmp_path / "dest.bin"
    with open(dest_path, "wb") as f:
        f.truncate(len(data))

    wfd = os.open(dest_path, os.O_RDWR)
    try:
        # verify_drive=False should execute write cleanly and skip read-back
        stream_zip_to_fd(
            archive_path=zip_path,
            member_name="test.bin",
            image_size=len(data),
            expected_sha1="",
            write_fd=wfd,
            verify_drive=False,
        )
    finally:
        os.close(wfd)

    with open(dest_path, "rb") as f:
        assert f.read() == data


def test_helper_daemon_session_lifecycle(tmp_path: Path) -> None:
    import secrets
    import subprocess
    import sys

    token = secrets.token_hex(32)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]

    src_root = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src_root}

    cmd = [sys.executable, "-m", "flexkit", "--helper-daemon", "--port", str(port), "--token", token]
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    try:
        srv.settimeout(5.0)
        conn, _ = srv.accept()
        conn.settimeout(5.0)

        # Handshake
        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "hello"
        assert msg["token"] == token

        # Test ping
        conn.sendall(b'{"cmd":"ping"}\n')
        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "pong"

        # Test quit
        conn.sendall(b'{"cmd":"quit"}\n')
        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "bye"

        assert proc.wait(timeout=3.0) == 0
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()

