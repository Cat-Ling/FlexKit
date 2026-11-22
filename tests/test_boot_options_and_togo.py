from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import uuid
import zipfile

import pytest

from flexkit.writer import (
    DISABLE_UPDATES_LSB_RELEASE,
    WriteError,
    stream_zip_to_fd,
    write_image_to_block_io,
)
from flexkit.writer.ext4 import format_ext4_partition, verify_ext4_etc_lsb_release
from flexkit.writer.flexshim import (
    install_flexshim_to_esp,
    load_flexshim_efi,
    render_flexshim_cfg,
    verify_flexshim_on_esp,
)
from flexkit.writer.gpt import (
    CHROMEOS_KERN_TYPE,
    CHROMEOS_RESERVED_TYPE,
    CHROMEOS_ROOT_TYPE,
    CHROMEOS_RWFW_TYPE,
    CHROMEOS_STATE_TYPE,
    EFI_LEGACY_BOOT_FLAG,
    EFI_SYSTEM_TYPE,
    GPT_ENTRY_SIZE,
    GPT_NUM_ENTRIES,
    KERN_A_INSTALL_FLAGS,
    KERN_INACTIVE_FLAGS,
    SECTOR_SIZE,
    GptPartition,
    GptTable,
    build_protective_mbr,
    plan_chromeos_togo_layout,
    read_gpt_table,
    read_gpt_table_from_bytes,
    serialize_gpt,
)

_O_BIN = getattr(os, "O_BINARY", 0)


def _fd_pwrite(fd: int, data: bytes | memoryview, offset: int) -> None:
    view = memoryview(data)
    pos = 0
    pwrite = getattr(os, "pwrite", None)
    while pos < len(view):
        if pwrite is not None:
            n = pwrite(fd, view[pos:], offset + pos)
        else:
            os.lseek(fd, offset + pos, os.SEEK_SET)
            n = os.write(fd, view[pos:])
        assert n > 0
        pos += n


def _fd_pread(fd: int, count: int, offset: int) -> bytes:
    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(fd, count, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, count)


class _SparseMemoryBlockIO:
    _PAGE_SIZE = 65536

    def __init__(self, size: int) -> None:
        self._size = size
        self._pages: dict[int, bytearray] = {}

    @property
    def size(self) -> int:
        return self._size

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        assert offset % SECTOR_SIZE == 0, f"Unaligned write offset {offset}"
        view = memoryview(data)
        assert len(view) % SECTOR_SIZE == 0, f"Unaligned write length {len(view)}"
        assert 0 <= offset and offset + len(view) <= self._size

        pos = 0
        while pos < len(view):
            cur = offset + pos
            page_idx = cur // self._PAGE_SIZE
            page_off = cur % self._PAGE_SIZE
            step = min(self._PAGE_SIZE - page_off, len(view) - pos)
            page = self._pages.get(page_idx)
            if page is None:
                page = bytearray(self._PAGE_SIZE)
                self._pages[page_idx] = page
            page[page_off : page_off + step] = view[pos : pos + step]
            pos += step

    def read_at(self, offset: int, size: int) -> bytes:
        assert 0 <= offset and offset + size <= self._size
        out = bytearray(size)
        pos = 0
        while pos < size:
            cur = offset + pos
            page_idx = cur // self._PAGE_SIZE
            page_off = cur % self._PAGE_SIZE
            step = min(self._PAGE_SIZE - page_off, size - pos)
            page = self._pages.get(page_idx)
            if page is not None:
                out[pos : pos + step] = page[page_off : page_off + step]
            pos += step
        return bytes(out)

    def flush(self) -> None:
        pass


def _find_sys_tool(name: str) -> str | None:
    for candidate in (shutil.which(name), f"/usr/sbin/{name}", f"/sbin/{name}"):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _build_minimal_fat16_esp(
    *,
    size_bytes: int = 4 * 1024 * 1024,
    crdyboot_payload: bytes = b"MZ_FAKE_CRDYBOOT",
    orig_boot_payload: bytes = b"MZ_ORIG_BOOTX64",
) -> bytes:
    assert size_bytes % SECTOR_SIZE == 0
    bps = 512
    spc = 1
    rsvd = 1
    nfats = 2
    root_cnt = 512
    tot_secs = size_bytes // bps
    spf = 32
    root_secs = (root_cnt * 32) // bps
    first_data = rsvd + nfats * spf + root_secs
    assert 4085 <= (tot_secs - first_data) // spc < 65525

    img = bytearray(size_bytes)
    struct.pack_into(
        "<3s8sHBHBHHBHHHIII",
        img,
        0,
        b"\xEB\x3C\x90",
        b"MSDOS5.0",
        bps,
        spc,
        rsvd,
        nfats,
        root_cnt,
        tot_secs if tot_secs < 65536 else 0,
        0xF8,
        spf,
        32,
        64,
        0,
        0 if tot_secs < 65536 else tot_secs,
        0x80,
    )
    img[54:62] = b"FAT16   "
    img[510:512] = b"\x55\xAA"

    fat = bytearray(spf * bps)
    struct.pack_into("<HH", fat, 0, 0xFFF8, 0xFFFF)
    # Cluster 2: /EFI dir; Cluster 3: /EFI/BOOT dir; Cluster 4: bootx64.efi; Cluster 5..: crdybootx64.efi
    struct.pack_into("<HH", fat, 2 * 2, 0xFFFF, 0xFFFF)
    struct.pack_into("<H", fat, 4 * 2, 0xFFFF)

    crdy_clusters = max(1, (len(crdyboot_payload) + 511) // 512)
    for idx in range(crdy_clusters):
        c = 5 + idx
        nxt = (c + 1) if idx + 1 < crdy_clusters else 0xFFFF
        struct.pack_into("<H", fat, c * 2, nxt)

    for copy_idx in range(nfats):
        fat_off = (rsvd + copy_idx * spf) * bps
        img[fat_off : fat_off + len(fat)] = fat

    def clus_off(c: int) -> int:
        return (first_data + (c - 2) * spc) * bps

    def pack_ent(name11: bytes, attr: int, clus: int, sz: int) -> bytes:
        return struct.pack("<11sBBBHHHHHHHI", name11, attr, 0, 0, 0, 0x5C21, 0x5C21, 0, 0, 0x5C21, clus, sz)

    root_off = (rsvd + nfats * spf) * bps
    img[root_off : root_off + 32] = pack_ent(b"EFI        ", 0x10, 2, 0)

    c2 = clus_off(2)
    img[c2 : c2 + 32] = pack_ent(b".          ", 0x10, 2, 0)
    img[c2 + 32 : c2 + 64] = pack_ent(b"..         ", 0x10, 0, 0)
    img[c2 + 64 : c2 + 96] = pack_ent(b"BOOT       ", 0x10, 3, 0)

    c3 = clus_off(3)
    img[c3 : c3 + 32] = pack_ent(b".          ", 0x10, 3, 0)
    img[c3 + 32 : c3 + 64] = pack_ent(b"..         ", 0x10, 2, 0)
    img[c3 + 64 : c3 + 96] = pack_ent(b"BOOTX64 EFI", 0x20, 4, len(orig_boot_payload))

    from flexkit.writer.flexshim import _encode_lfn_entries, _lfn_checksum

    s11 = b"CRDYBO~1EFI"
    lfn_recs = _encode_lfn_entries("crdybootx64.efi", _lfn_checksum(s11))
    pos = c3 + 96
    for rec in lfn_recs:
        img[pos : pos + 32] = rec
        pos += 32
    img[pos : pos + 32] = pack_ent(s11, 0x20, 5, len(crdyboot_payload))

    img[clus_off(4) : clus_off(4) + len(orig_boot_payload)] = orig_boot_payload
    img[clus_off(5) : clus_off(5) + len(crdyboot_payload)] = crdyboot_payload
    return bytes(img)


def _make_installer_gpt(
    *,
    kern_a_guid: uuid.UUID | None = None,
    kern_b_guid: uuid.UUID | None = None,
    root_a_guid: uuid.UUID | None = None,
    root_b_guid: uuid.UUID | None = None,
    efi_guid: uuid.UUID | None = None,
    oem_guid: uuid.UUID | None = None,
    state_guid: uuid.UUID | None = None,
    kern_sectors: int = 131072,
    oem_sectors: int = 32768,
    efi_sectors: int = 524288,
    root_a_sectors: int = 4915200,
    total_sectors: int = 18600121,
) -> GptTable:
    parts = (
        GptPartition(11, "RWFW", CHROMEOS_RWFW_TYPE, uuid.uuid4(), 64, 64, 0),
        GptPartition(6, "KERN-C", CHROMEOS_KERN_TYPE, uuid.uuid4(), 65, 65, 0),
        GptPartition(7, "ROOT-C", CHROMEOS_ROOT_TYPE, uuid.uuid4(), 66, 66, 0),
        GptPartition(9, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), 67, 67, 0),
        GptPartition(10, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), 68, 68, 0),
        GptPartition(2, "KERN-A", CHROMEOS_KERN_TYPE, kern_a_guid or uuid.uuid4(), 69, 69 + kern_sectors - 1, KERN_A_INSTALL_FLAGS),
        GptPartition(4, "KERN-B", CHROMEOS_KERN_TYPE, kern_b_guid or uuid.uuid4(), 69 + kern_sectors, 69 + 2 * kern_sectors - 1, 0),
        GptPartition(8, "OEM", CHROMEOS_STATE_TYPE, oem_guid or uuid.uuid4(), 266240, 266240 + oem_sectors - 1, 0),
        GptPartition(12, "EFI-SYSTEM", EFI_SYSTEM_TYPE, efi_guid or uuid.uuid4(), 299008, 299008 + efi_sectors - 1, EFI_LEGACY_BOOT_FLAG),
        GptPartition(5, "ROOT-B", CHROMEOS_ROOT_TYPE, root_b_guid or uuid.uuid4(), 823296, 823296 + 4096 - 1, 0),
        GptPartition(3, "ROOT-A", CHROMEOS_ROOT_TYPE, root_a_guid or uuid.uuid4(), 827392, 827392 + root_a_sectors - 1, 0),
        GptPartition(1, "STATE", CHROMEOS_STATE_TYPE, state_guid or uuid.uuid4(), 6004736, total_sectors - 34, 0),
    )
    return GptTable(
        disk_guid=uuid.uuid4(),
        total_sectors=total_sectors,
        first_usable_lba=34,
        last_usable_lba=total_sectors - 34,
        partitions=parts,
    )


def test_ext4_format_passes_e2fsck(tmp_path: Path) -> None:
    e2fsck = _find_sys_tool("e2fsck")
    dumpe2fs = _find_sys_tool("dumpe2fs")
    if e2fsck is None or dumpe2fs is None:
        pytest.skip("e2fsck/dumpe2fs not installed")

    for size_bytes in (256 * 1024 * 1024, 6 * 1024 * 1024 * 1024, 64 * 1024 * 1024 * 1024):
        img = tmp_path / f"hstate_{size_bytes}.img"
        fd = os.open(img, os.O_RDWR | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
        try:
            os.ftruncate(fd, size_bytes)
            format_ext4_partition(lambda off, d: _fd_pwrite(fd, d, off), 0, size_bytes, label="H-STATE")
            os.fsync(fd)
        finally:
            os.close(fd)

        chk = subprocess.run([e2fsck, "-fn", str(img)], capture_output=True, text=True)
        assert chk.returncode == 0, f"e2fsck failed ({size_bytes}): {chk.stdout}\n{chk.stderr}"
        img.unlink()

    meta_img = tmp_path / "hstate_meta.img"
    fd = os.open(meta_img, os.O_RDWR | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
    try:
        os.ftruncate(fd, 256 * 1024 * 1024)
        format_ext4_partition(lambda off, d: _fd_pwrite(fd, d, off), 0, 256 * 1024 * 1024, label="H-STATE")
        os.fsync(fd)
    finally:
        os.close(fd)

    dump = subprocess.check_output([dumpe2fs, "-h", str(meta_img)], text=True, stderr=subprocess.STDOUT)
    assert "Filesystem volume name:   H-STATE" in dump
    for feat in ("has_journal", "encrypt", "verity", "metadata_csum", "64bit", "extent"):
        assert feat in dump


def test_ext4_format_with_etc_lsb_release_passes_e2fsck_and_debugfs(tmp_path: Path) -> None:
    e2fsck = _find_sys_tool("e2fsck")
    debugfs = _find_sys_tool("debugfs")
    if e2fsck is None:
        pytest.skip("e2fsck not installed")

    with pytest.raises(ValueError, match="etc_lsb_release"):
        format_ext4_partition(lambda _o, _d: None, 0, 256 * 1024 * 1024, etc_lsb_release=b"")

    img = tmp_path / "hstate_lsb.img"
    size_bytes = 256 * 1024 * 1024
    fd = os.open(img, os.O_RDWR | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
    try:
        os.ftruncate(fd, size_bytes)
        format_ext4_partition(
            lambda off, d: _fd_pwrite(fd, d, off),
            0,
            size_bytes,
            label="H-STATE",
            etc_lsb_release=DISABLE_UPDATES_LSB_RELEASE,
        )
        os.fsync(fd)
        verify_ext4_etc_lsb_release(lambda off, n: _fd_pread(fd, n, off), 0, DISABLE_UPDATES_LSB_RELEASE)
    finally:
        os.close(fd)

    chk = subprocess.run([e2fsck, "-fn", str(img)], capture_output=True, text=True)
    assert chk.returncode == 0, f"e2fsck failed with /etc/lsb-release: {chk.stdout}\n{chk.stderr}"

    if debugfs is not None:
        cat_res = subprocess.run([debugfs, "-R", "cat /etc/lsb-release", str(img)], capture_output=True)
        assert cat_res.returncode == 0
        assert cat_res.stdout == DISABLE_UPDATES_LSB_RELEASE

        stat_res = subprocess.run([debugfs, "-R", "stat /etc/lsb-release", str(img)], capture_output=True, text=True)
        assert stat_res.returncode == 0
        assert "Type: regular" in stat_res.stdout
        assert "0644" in stat_res.stdout

    fd = os.open(img, os.O_RDWR | _O_BIN)
    try:
        with pytest.raises(ValueError, match="size mismatch"):
            verify_ext4_etc_lsb_release(lambda off, n: _fd_pread(fd, n, off), 0, b"CHROMEOS_RELEASE_VERSION=1.0.0\n")
        with pytest.raises(ValueError, match="contents"):
            verify_ext4_etc_lsb_release(lambda off, n: _fd_pread(fd, n, off), 0, b"CHROMEOS_RELEASE_VERSION=12345.0.0\n")
    finally:
        os.close(fd)


def test_plan_chromeos_togo_layout_matches_write_base_table() -> None:
    ka_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    ra_id = uuid.uuid4()
    rb_id = uuid.uuid4()
    efi_id = uuid.uuid4()
    oem_id = uuid.uuid4()

    src_gpt = _make_installer_gpt(
        kern_a_guid=ka_id,
        kern_b_guid=kb_id,
        root_a_guid=ra_id,
        root_b_guid=rb_id,
        efi_guid=efi_id,
        oem_guid=oem_id,
    )

    total_sectors = 31250000
    parts = plan_chromeos_togo_layout(src_gpt, total_sectors)
    by_name = {p.name: p for p in parts}

    assert by_name["ROOT-B"].start_lba == 823296
    assert by_name["ROOT-B"].end_lba == 9211903
    assert by_name["ROOT-B"].sector_count == 8388608

    assert by_name["ROOT-A"].start_lba == 9211904
    assert by_name["ROOT-A"].end_lba == 17600511
    assert by_name["ROOT-A"].sector_count == 8388608
    assert by_name["ROOT-A"].sector_count == by_name["ROOT-B"].sector_count

    assert by_name["STATE"].start_lba == 17862656
    assert by_name["STATE"].end_lba == total_sectors - 49

    assert by_name["ROOT-A"].unique_guid == ra_id
    assert by_name["ROOT-B"].unique_guid == rb_id
    assert by_name["KERN-A"].unique_guid == ka_id
    assert by_name["KERN-B"].unique_guid == kb_id
    assert by_name["EFI-SYSTEM"].unique_guid == efi_id
    assert by_name["OEM"].unique_guid == oem_id

    assert by_name["KERN-A"].flags == KERN_A_INSTALL_FLAGS
    assert by_name["KERN-B"].flags == KERN_INACTIVE_FLAGS
    assert by_name["EFI-SYSTEM"].flags == EFI_LEGACY_BOOT_FLAG

    mbr = build_protective_mbr(
        total_sectors,
        efi_lba=by_name["EFI-SYSTEM"].start_lba,
        efi_sectors=by_name["EFI-SYSTEM"].sector_count,
    )
    assert len(mbr) == 512
    assert mbr[510:512] == b"\x55\xAA"
    status1, ptype1, lba1, secs1 = struct.unpack_from("<B3xB3xII", mbr, 446)
    assert status1 == 0x80
    assert ptype1 == 0x0C
    assert lba1 == 299008
    assert secs1 == 524288
    status2, ptype2 = struct.unpack_from("<B3xB", mbr, 462)
    assert status2 == 0x00
    assert ptype2 == 0xEE

    pri_hdr, entries, bak_hdr = serialize_gpt(total_sectors, parts)
    assert pri_hdr[:8] == b"EFI PART"
    assert bak_hdr[:8] == b"EFI PART"
    assert len(entries) == GPT_NUM_ENTRIES * GPT_ENTRY_SIZE

    with pytest.raises(ValueError, match="too small"):
        plan_chromeos_togo_layout(src_gpt, 15625000)


def _build_synthetic_installer_zip(
    zip_path: Path,
    *,
    boot_code: bytes,
    kern_payload: bytes,
    oem_payload: bytes,
    efi_payload: bytes,
    root_payload: bytes,
    kern_a_guid: uuid.UUID,
    kern_b_guid: uuid.UUID,
    root_a_guid: uuid.UUID,
    root_b_guid: uuid.UUID,
) -> int:
    kern_secs = len(kern_payload) // SECTOR_SIZE
    oem_secs = len(oem_payload) // SECTOR_SIZE
    efi_secs = len(efi_payload) // SECTOR_SIZE
    root_secs = len(root_payload) // SECTOR_SIZE

    p11_s = 64
    p6_s = 65
    p7_s = 66
    p9_s = 67
    p10_s = 68
    ka_s = 69
    kb_s = ka_s + kern_secs
    oem_s = ((kb_s + kern_secs + 4095) // 4096) * 4096
    efi_s = oem_s + oem_secs
    rb_s = ((efi_s + efi_secs + 4095) // 4096) * 4096
    ra_s = rb_s + 4096
    st_s = ra_s + root_secs
    total_secs = st_s + 2048 + 34

    parts = [
        GptPartition(11, "RWFW", CHROMEOS_RWFW_TYPE, uuid.uuid4(), p11_s, p11_s, 0),
        GptPartition(6, "KERN-C", CHROMEOS_KERN_TYPE, uuid.uuid4(), p6_s, p6_s, 0),
        GptPartition(7, "ROOT-C", CHROMEOS_ROOT_TYPE, uuid.uuid4(), p7_s, p7_s, 0),
        GptPartition(9, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), p9_s, p9_s, 0),
        GptPartition(10, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), p10_s, p10_s, 0),
        GptPartition(2, "KERN-A", CHROMEOS_KERN_TYPE, kern_a_guid, ka_s, ka_s + kern_secs - 1, KERN_A_INSTALL_FLAGS),
        GptPartition(4, "KERN-B", CHROMEOS_KERN_TYPE, kern_b_guid, kb_s, kb_s + kern_secs - 1, 0),
        GptPartition(8, "OEM", CHROMEOS_STATE_TYPE, uuid.uuid4(), oem_s, oem_s + oem_secs - 1, 0),
        GptPartition(12, "EFI-SYSTEM", EFI_SYSTEM_TYPE, uuid.uuid4(), efi_s, efi_s + efi_secs - 1, EFI_LEGACY_BOOT_FLAG),
        GptPartition(5, "ROOT-B", CHROMEOS_ROOT_TYPE, root_b_guid, rb_s, rb_s + 4095, 0),
        GptPartition(3, "ROOT-A", CHROMEOS_ROOT_TYPE, root_a_guid, ra_s, ra_s + root_secs - 1, 0),
        GptPartition(1, "STATE", CHROMEOS_STATE_TYPE, uuid.uuid4(), st_s, total_secs - 34, 0),
    ]

    raw_img = bytearray(total_secs * SECTOR_SIZE)
    mbr = build_protective_mbr(total_secs, boot_code=boot_code, efi_lba=efi_s, efi_sectors=efi_secs)
    pri, ent, bak = serialize_gpt(total_secs, parts)
    raw_img[0:512] = mbr
    raw_img[512:1024] = pri
    raw_img[1024 : 1024 + len(ent)] = ent
    raw_img[(total_secs - 33) * SECTOR_SIZE : (total_secs - 1) * SECTOR_SIZE] = ent
    raw_img[(total_secs - 1) * SECTOR_SIZE : total_secs * SECTOR_SIZE] = bak

    raw_img[kb_s * SECTOR_SIZE : (kb_s + kern_secs) * SECTOR_SIZE] = kern_payload
    raw_img[oem_s * SECTOR_SIZE : (oem_s + oem_secs) * SECTOR_SIZE] = oem_payload
    raw_img[efi_s * SECTOR_SIZE : (efi_s + efi_secs) * SECTOR_SIZE] = efi_payload
    raw_img[ra_s * SECTOR_SIZE : (ra_s + root_secs) * SECTOR_SIZE] = root_payload

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("chromeos.bin", bytes(raw_img))
    return len(raw_img)


def test_togo_fresh_and_update_stream_and_verify(tmp_path: Path) -> None:
    boot_code = b"\xEB\x3C\x90FLEXBOOT" + b"\x00" * (446 - 11)
    r1_ka = uuid.uuid4()
    r1_kb = uuid.uuid4()
    r1_ra = uuid.uuid4()
    r1_rb = uuid.uuid4()

    kern1 = b"KERN_V1_" * ((128 * 1024) // 8)
    oem1 = b"OEM_V1__" * ((64 * 1024) // 8)
    efi1 = b"EFI_V1__" * ((256 * 1024) // 8)
    root1 = b"ROOT_V1_" * ((512 * 1024) // 8)

    zip1 = tmp_path / "flex_v1.zip"
    img1_size = _build_synthetic_installer_zip(
        zip1,
        boot_code=boot_code,
        kern_payload=kern1,
        oem_payload=oem1,
        efi_payload=efi1,
        root_payload=root1,
        kern_a_guid=r1_ka,
        kern_b_guid=r1_kb,
        root_a_guid=r1_ra,
        root_b_guid=r1_rb,
    )

    target_size = 12 * 1024 * 1024 * 1024
    mem_io = _SparseMemoryBlockIO(target_size)
    write_image_to_block_io(
        archive_path=zip1,
        member_name="chromeos.bin",
        image_size=img1_size,
        expected_sha1="",
        io=mem_io,
        verify_drive=True,
        install_mode="to_go_fresh",
        expected_size=target_size,
    )

    mbr = mem_io.read_at(0, SECTOR_SIZE)
    assert mbr[:446] == boot_code
    assert mbr[510:512] == b"\x55\xAA"
    gpt1 = read_gpt_table_from_bytes(mem_io.read_at(0, 34 * SECTOR_SIZE))
    assert gpt1 is not None and gpt1.is_chromeos_togo is True

    p_ka = gpt1.get_by_name("KERN-A")
    p_kb = gpt1.get_by_name("KERN-B")
    p_oem = gpt1.get_by_name("OEM")
    p_efi = gpt1.get_by_name("EFI-SYSTEM")
    p_ra = gpt1.get_by_name("ROOT-A")
    p_rb = gpt1.get_by_name("ROOT-B")
    p_st = gpt1.get_by_name("STATE")
    assert p_ka and p_kb and p_oem and p_efi and p_ra and p_rb and p_st
    assert p_ka.unique_guid == r1_ka
    assert p_kb.unique_guid == r1_kb
    assert p_ra.unique_guid == r1_ra
    assert p_rb.unique_guid == r1_rb
    assert p_ra.sector_count == p_rb.sector_count

    for part, expected_bytes in (
        (p_ka, kern1),
        (p_kb, kern1),
        (p_oem, oem1),
        (p_efi, efi1),
        (p_ra, root1),
        (p_rb, root1),
    ):
        assert mem_io.read_at(part.start_lba * SECTOR_SIZE, len(expected_bytes)) == expected_bytes

    sentinel_offset = p_st.start_lba * SECTOR_SIZE + (64 * 1024 * 1024)
    sentinel_data = (b"USER_PERSISTENT_DATA_DO_NOT_OVERWRITE" * 16).ljust(SECTOR_SIZE * 2, b"\x00")
    mem_io.write_at(sentinel_offset, sentinel_data)

    r2_ka = uuid.uuid4()
    r2_kb = uuid.uuid4()
    r2_ra = uuid.uuid4()
    r2_rb = uuid.uuid4()
    kern2 = b"KERN_V2_" * ((128 * 1024) // 8)
    oem2 = b"OEM_V2__" * ((64 * 1024) // 8)
    efi2 = b"EFI_V2__" * ((256 * 1024) // 8)
    root2 = b"ROOT_V2_" * ((512 * 1024) // 8)
    zip2 = tmp_path / "flex_v2.zip"
    img2_size = _build_synthetic_installer_zip(
        zip2,
        boot_code=boot_code,
        kern_payload=kern2,
        oem_payload=oem2,
        efi_payload=efi2,
        root_payload=root2,
        kern_a_guid=r2_ka,
        kern_b_guid=r2_kb,
        root_a_guid=r2_ra,
        root_b_guid=r2_rb,
    )

    write_image_to_block_io(
        archive_path=zip2,
        member_name="chromeos.bin",
        image_size=img2_size,
        expected_sha1="",
        io=mem_io,
        verify_drive=True,
        install_mode="to_go_update",
        expected_size=target_size,
    )

    gpt2 = read_gpt_table_from_bytes(mem_io.read_at(0, 34 * SECTOR_SIZE))
    assert gpt2 is not None and gpt2.is_chromeos_togo is True
    p2_ra = gpt2.get_by_name("ROOT-A")
    p2_rb = gpt2.get_by_name("ROOT-B")
    p2_st = gpt2.get_by_name("STATE")
    assert p2_ra and p2_rb and p2_st
    assert p2_ra.unique_guid == r2_ra
    assert p2_rb.unique_guid == r2_rb
    assert p2_st.unique_guid == p_st.unique_guid
    assert p2_st.start_lba == p_st.start_lba
    assert mem_io.read_at(p2_ra.start_lba * SECTOR_SIZE, len(root2)) == root2
    assert mem_io.read_at(p2_rb.start_lba * SECTOR_SIZE, len(root2)) == root2
    assert mem_io.read_at(sentinel_offset, len(sentinel_data)) == sentinel_data

    def flip_root_a() -> None:
        sec_off = p2_ra.start_lba * SECTOR_SIZE + 4096
        orig = bytearray(mem_io.read_at(sec_off, SECTOR_SIZE))
        orig[0] ^= 0x01
        mem_io.write_at(sec_off, orig)

    with pytest.raises(WriteError, match="Verification failed"):
        write_image_to_block_io(
            archive_path=zip2,
            member_name="chromeos.bin",
            image_size=img2_size,
            expected_sha1="",
            io=mem_io,
            before_verify_hook=flip_root_a,
            verify_drive=True,
            install_mode="to_go_fresh",
            expected_size=target_size,
        )

    inst_gpt = _make_installer_gpt()
    pri_i, ent_i, _bak_i = serialize_gpt(inst_gpt.total_sectors, inst_gpt.partitions)
    inst_io = _SparseMemoryBlockIO(inst_gpt.total_sectors * SECTOR_SIZE)
    inst_io.write_at(0, build_protective_mbr(inst_gpt.total_sectors))
    inst_io.write_at(SECTOR_SIZE, pri_i)
    inst_io.write_at(2 * SECTOR_SIZE, ent_i)
    with pytest.raises(WriteError, match="valid ChromeOS To Go"):
        write_image_to_block_io(
            archive_path=zip1,
            member_name="chromeos.bin",
            image_size=img1_size,
            expected_sha1="",
            io=inst_io,
            verify_drive=False,
            install_mode="to_go_update",
            expected_size=inst_gpt.total_sectors * SECTOR_SIZE,
        )


def test_stream_zip_to_fd_skip_verify(tmp_path: Path) -> None:
    data = b"X" * (1024 * 1024)
    zip_path = tmp_path / "img.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("test.bin", data)

    dest_path = tmp_path / "dest.bin"
    with open(dest_path, "wb") as f:
        f.truncate(len(data))

    wfd = os.open(dest_path, os.O_RDWR | _O_BIN)
    try:
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


def test_render_flexshim_cfg_toggles_cros_debug() -> None:
    disabled = render_flexshim_cfg(cros_debug=False).decode("utf-8")
    assert "# cros_debug=true" in disabled
    assert "\ncros_debug=true\n" not in f"\n{disabled}"

    enabled = render_flexshim_cfg(cros_debug=True).decode("utf-8")
    assert "\ncros_debug=true\n" in f"\n{enabled}"
    assert "flexshim configuration file" in enabled

    re_disabled = render_flexshim_cfg(cros_debug=False, template=enabled).decode("utf-8")
    assert "# cros_debug=true" in re_disabled
    assert "\ncros_debug=true\n" not in f"\n{re_disabled}"


def test_install_flexshim_on_fat16_and_fat32_esp(tmp_path: Path) -> None:
    efi_bin = load_flexshim_efi()
    assert efi_bin[:2] == b"MZ"

    raw_esp = _build_minimal_fat16_esp()
    esp_io = _SparseMemoryBlockIO(len(raw_esp))
    esp_io.write_at(0, raw_esp)

    installed_efi, installed_cfg = install_flexshim_to_esp(esp_io, 0, len(raw_esp), cros_debug=False)
    assert installed_efi == efi_bin
    assert b"# cros_debug=true" in installed_cfg
    verify_flexshim_on_esp(esp_io, 0, len(raw_esp), installed_efi, installed_cfg)

    updated_efi, updated_cfg = install_flexshim_to_esp(esp_io, 0, len(raw_esp), cros_debug=True)
    assert b"\ncros_debug=true\n" in b"\n" + updated_cfg
    verify_flexshim_on_esp(esp_io, 0, len(raw_esp), updated_efi, updated_cfg)

    no_crdy_esp = _build_minimal_fat16_esp().replace(b"crdybootx64.efi", b"otherboot64.efi")
    # Remove crdyboot entry cleanly by zeroing LFN+8.3 entry in cluster 3.
    no_crdy_io = _SparseMemoryBlockIO(len(raw_esp))
    no_crdy_buf = bytearray(raw_esp)
    crdy_clus_off = (1 + 2 * 32 + 32 + 1) * 512
    no_crdy_buf[crdy_clus_off + 96 : crdy_clus_off + 192] = b"\x00" * 96
    no_crdy_io.write_at(0, no_crdy_buf)
    with pytest.raises(WriteError, match="crdybootx64.efi"):
        install_flexshim_to_esp(no_crdy_io, 0, len(no_crdy_buf), cros_debug=False)

    mkfs = _find_sys_tool("mkfs.vfat")
    mmd = _find_sys_tool("mmd")
    mcopy = _find_sys_tool("mcopy")
    fsck = _find_sys_tool("fsck.vfat")
    if mkfs and mmd and mcopy and fsck:
        for fat_bits, size_mb in ((16, 16), (32, 36)):
            esp_path = tmp_path / f"esp_fat{fat_bits}.img"
            esp_bytes = size_mb * 1024 * 1024
            esp_path.write_bytes(b"\x00" * esp_bytes)
            subprocess.run([mkfs, "-F", str(fat_bits), "-n", "EFI-SYSTEM", str(esp_path)], check=True)
            subprocess.run([mmd, "-i", str(esp_path), "::/efi", "::/efi/boot"], check=True)
            dummy_boot = tmp_path / "orig_bootx64.efi"
            dummy_boot.write_bytes(b"MZ" + b"\x00" * 4096)
            dummy_crdy = tmp_path / "crdybootx64.efi"
            dummy_crdy.write_bytes(b"MZCRDY" + b"\x55" * 16384)
            subprocess.run([mcopy, "-i", str(esp_path), str(dummy_boot), "::/efi/boot/bootx64.efi"], check=True)
            subprocess.run([mcopy, "-i", str(esp_path), str(dummy_crdy), "::/efi/boot/crdybootx64.efi"], check=True)

            mem = _SparseMemoryBlockIO(esp_bytes)
            mem.write_at(0, esp_path.read_bytes())
            got_efi, got_cfg = install_flexshim_to_esp(mem, 0, esp_bytes, cros_debug=True)
            verify_flexshim_on_esp(mem, 0, esp_bytes, got_efi, got_cfg)
            esp_path.write_bytes(mem.read_at(0, esp_bytes))

            chk = subprocess.run([fsck, "-n", str(esp_path)], capture_output=True, text=True)
            assert chk.returncode == 0, f"fsck.vfat FAT{fat_bits} failed: {chk.stdout}\n{chk.stderr}"


def test_installer_and_togo_stream_with_flexshim(tmp_path: Path) -> None:
    crdy_payload = b"MZCRDYBOOT_VERIFIED_BIN_" * 200
    esp_payload = _build_minimal_fat16_esp(size_bytes=4 * 1024 * 1024, crdyboot_payload=crdy_payload)
    boot_code = b"\xEB\x3C\x90FLEXBOOT" + b"\x00" * (446 - 11)
    kern_payload = b"KERN_SHIM_123456" * ((128 * 1024) // 16)
    oem_payload = b"OEM_SHIM__123456" * ((64 * 1024) // 16)
    root_payload = b"ROOT_SHIM_123456" * ((512 * 1024) // 16)

    zip_path = tmp_path / "flex_with_esp.zip"
    img_size = _build_synthetic_installer_zip(
        zip_path,
        boot_code=boot_code,
        kern_payload=kern_payload,
        oem_payload=oem_payload,
        efi_payload=esp_payload,
        root_payload=root_payload,
        kern_a_guid=uuid.uuid4(),
        kern_b_guid=uuid.uuid4(),
        root_a_guid=uuid.uuid4(),
        root_b_guid=uuid.uuid4(),
    )

    inst_io = _SparseMemoryBlockIO(img_size)
    with pytest.raises(WriteError, match="ChromeOS To Go fresh"):
        write_image_to_block_io(
            archive_path=zip_path,
            member_name="chromeos.bin",
            image_size=img_size,
            expected_sha1="",
            io=inst_io,
            verify_drive=False,
            install_mode="installer",
            expected_size=img_size,
            disable_updates=True,
        )

    write_image_to_block_io(
        archive_path=zip_path,
        member_name="chromeos.bin",
        image_size=img_size,
        expected_sha1="",
        io=inst_io,
        verify_drive=True,
        install_mode="installer",
        expected_size=img_size,
        install_flexshim=True,
        cros_debug=True,
    )
    inst_gpt = read_gpt_table_from_bytes(inst_io.read_at(0, 34 * SECTOR_SIZE))
    assert inst_gpt is not None
    inst_efi = inst_gpt.get_by_name("EFI-SYSTEM")
    assert inst_efi is not None
    expected_efi = load_flexshim_efi()
    expected_cfg = render_flexshim_cfg(cros_debug=True)
    verify_flexshim_on_esp(inst_io, inst_efi.start_lba * SECTOR_SIZE, inst_efi.size_bytes, expected_efi, expected_cfg)

    togo_size = 12 * 1024 * 1024 * 1024
    togo_io = _SparseMemoryBlockIO(togo_size)
    write_image_to_block_io(
        archive_path=zip_path,
        member_name="chromeos.bin",
        image_size=img_size,
        expected_sha1="",
        io=togo_io,
        verify_drive=True,
        install_mode="to_go_fresh",
        expected_size=togo_size,
        install_flexshim=False,
        cros_debug=False,
        disable_updates=True,
    )
    togo_gpt = read_gpt_table_from_bytes(togo_io.read_at(0, 34 * SECTOR_SIZE))
    assert togo_gpt is not None
    togo_efi = togo_gpt.get_by_name("EFI-SYSTEM")
    togo_state = togo_gpt.get_by_name("STATE")
    assert togo_efi is not None and togo_state is not None
    verify_flexshim_on_esp(togo_io, togo_efi.start_lba * SECTOR_SIZE, togo_efi.size_bytes, expected_efi, expected_cfg)
    verify_ext4_etc_lsb_release(
        togo_io.read_at,
        togo_state.start_lba * SECTOR_SIZE,
        DISABLE_UPDATES_LSB_RELEASE,
    )

    with pytest.raises(WriteError, match="ChromeOS To Go fresh"):
        write_image_to_block_io(
            archive_path=zip_path,
            member_name="chromeos.bin",
            image_size=img_size,
            expected_sha1="",
            io=togo_io,
            verify_drive=False,
            install_mode="to_go_update",
            expected_size=togo_size,
            disable_updates=True,
        )


def test_drive_page_flexshim_option_coupling() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from flexkit.drives import Drive, DriveEntry
    from flexkit.ui.pages import DrivePage
    from flexkit.ui.qt import QtWidgets

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = _app
    page = DrivePage()

    assert page.install_mode() == "installer"
    assert page.install_flexshim_checked() is False
    assert page.cros_debug_checked() is False
    assert page.disable_updates_checked() is False
    assert page.disable_updates_tile.isEnabled() is False

    page.cros_debug_tile.setChecked(True)
    assert page.flexshim_tile.isChecked() is True
    assert page.install_flexshim_checked() is True
    assert page.cros_debug_checked() is True
    assert page.disable_updates_checked() is False

    page.flexshim_tile.setChecked(False)
    assert page.cros_debug_tile.isChecked() is False
    assert page.install_flexshim_checked() is False
    assert page.cros_debug_checked() is False

    page.mode_dropdown.set_current_index(1)
    assert page.install_mode() == "to_go_fresh"
    assert page.disable_updates_tile.isEnabled() is True

    page.disable_updates_tile.setChecked(True)
    assert page.flexshim_tile.isChecked() is True
    assert page.cros_debug_tile.isChecked() is True
    assert page.install_flexshim_checked() is True
    assert page.cros_debug_checked() is True
    assert page.disable_updates_checked() is True

    page.cros_debug_tile.setChecked(False)
    assert page.disable_updates_tile.isChecked() is False
    assert page.disable_updates_checked() is False
    assert page.flexshim_tile.isChecked() is True

    cros_drive = Drive(
        id="/dev/sdb",
        name="SanDisk Extreme",
        size=64 * 1024 * 1024 * 1024,
        bus="USB",
        removable=True,
        serial="SER123",
        volumes=(),
        system=False,
        read_only=False,
        has_chromeos=True,
    )
    page.set_entries([DriveEntry(drive=cros_drive)])
    assert page.install_mode() == "to_go_update"
    assert page.disable_updates_tile.isEnabled() is False
    assert page.disable_updates_checked() is False

    page.keep_data_tile.setChecked(False)
    assert page.install_mode() == "to_go_fresh"
    assert page.disable_updates_tile.isEnabled() is True
    page.disable_updates_tile.setChecked(True)
    assert page.disable_updates_checked() is True
    assert page.cros_debug_checked() is True

    page.keep_data_tile.setChecked(True)
    assert page.install_mode() == "to_go_update"
    assert page.disable_updates_tile.isEnabled() is False
    assert page.disable_updates_tile.isChecked() is False
    assert page.disable_updates_checked() is False
    assert page.cros_debug_checked() is True

    page.flexshim_tile.setChecked(False)
    assert page.cros_debug_tile.isChecked() is False
    assert page.install_flexshim_checked() is False


def test_build_script_target_commands_and_icons(tmp_path: Path) -> None:
    build_py = Path(__file__).resolve().parent.parent / "scripts" / "build.py"
    spec = importlib.util.spec_from_file_location("flexkit_build_script", build_py)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    icons = mod.ensure_platform_icons(tmp_path / "icons")
    assert icons["png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert icons["ico"].read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert icons["icns"].read_bytes()[:4] == b"icns"

    for plat, qt_plugin, expected_flag in (
        ("linux", "pyside6", "--onefile"),
        ("win32", "pyside6", "--windows-console-mode=disable"),
        ("darwin", "pyside6", "--macos-create-app-bundle"),
    ):
        cmd = mod.build_nuitka_command(
            python_exe=sys.executable,
            target_platform=plat,
            qt_plugin=qt_plugin,
            work_dir=tmp_path / "work",
            icons=icons,
            stem="flexkit-0.0.1-test-x86_64",
        )
        assert expected_flag in cmd
        assert any(arg.startswith("--include-data-dir=") for arg in cmd)
        assert "--output-filename=flexkit" not in cmd
        if plat == "darwin":
            assert "--output-filename=flexkit.bin" in cmd


def test_helper_daemon_session_lifecycle(tmp_path: Path) -> None:
    import secrets

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

        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "hello"
        assert msg["token"] == token

        conn.sendall(b'{"cmd":"ping"}\n')
        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "pong"

        conn.sendall(b'{"cmd":"quit"}\n')
        line = conn.recv(1024).decode("utf-8")
        msg = json.loads(line)
        assert msg["event"] == "bye"

        assert proc.wait(timeout=3.0) == 0
    finally:
        srv.close()
        if proc.poll() is None:
            proc.kill()


def test_log_flag_configuration(tmp_path: Path) -> None:
    from flexkit import configure_logging, set_log_settings

    log_path = tmp_path / "test_log.txt"
    lvl, path = set_log_settings("debug", log_path)
    assert lvl == "DEBUG"
    assert path == log_path.resolve()

    logger = configure_logging(log_level="DEBUG", log_file=log_path)
    logger.debug("Test debug message 12345")
    logger.info("Test info message 67890")

    for h in logger.handlers:
        h.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "Test debug message 12345" in content
    assert "Test info message 67890" in content
