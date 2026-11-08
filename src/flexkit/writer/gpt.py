from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import struct
from typing import BinaryIO
import uuid
import zlib

SECTOR_SIZE = 512
GPT_HEADER_SIZE = 92
GPT_NUM_ENTRIES = 128
GPT_ENTRY_SIZE = 128

CHROMEOS_KERN_TYPE = uuid.UUID("fe3a2a5d-4f32-41a7-b725-accc3285a309")
CHROMEOS_ROOT_TYPE = uuid.UUID("3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec")
CHROMEOS_STATE_TYPE = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
EFI_SYSTEM_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
CHROMEOS_RWFW_TYPE = uuid.UUID("cab6e88e-abf3-4102-a07a-d4bb9be3c1d3")
CHROMEOS_RESERVED_TYPE = uuid.UUID("2e0a753d-9e48-43b0-8337-b15192cb1b5e")

ROOT_A_UNIQUE_UUID = uuid.UUID("fdecd5e5-f2f2-ce47-852b-3839e867212b")
ROOT_B_UNIQUE_UUID = uuid.UUID("c99b551e-01ac-7345-864f-a9fdbd74f6c1")

CHROMEOS_HYBRID_MBR_CODE = (
    b"3\xc0\xfa\x8e\xd8\x8e\xd0\xbc\x00|\x89\xe6\x06W\x8e\xc0\xfb\xfc\xbf\x00\x06\xb9\x00\x01\xf3\xa5\xea\x1f\x06\x00\x00"
    b"R\x89\xe5\x83\xec\x1cj\x1e\xc7F\xfa\x00\x02R\xb4A\xbb\xaaU1\xc90\xf6\xf9\xcd\x13Z\xb4\x08r\x17\x81\xfbU\xaau\x11"
    b"\xd1\xe9s\rf\xc7\x06Y\x07\xb4B\xeb\x13\xb4H\x89\xe6\xcd\x13\x83\xe1?Q\x0f\xb6\xc6@\xf7\xe1RPf1\xc0f\x99@\xe8\xdc\x00"
    b"\x8bNV\x8bFZPQ\xf7\xe1\xf7v\xfa\x91Af\x8bFNf\x8bVRS\xe8\xc4\x00\xe2\xfb1\xf6_YXf\x8b\x15f\x0bU\x04f\x0bU\x08f\x0bU\x0c"
    b"t\x0c\xf6E0\x04t\x06!\xf6u\x19\x89\xfe\x01\xc7\xe2\xdf!\xf6u.\xe8\xe1\x00Missing OS\r\n\xe8\xd2\x00Multiple active partitions\r\n"
    b"\x91\xbf\xbe\x07Wf1\xc0\xb0\x80f\xab\xb0\xedf\xabf\x8bD f\x8bT$\xe8@\x00f\x8bD(f\x8bT,f+D f\x1bT$\xe8p\x00\xe8*\x00"
    b"f\x0f\xb7\xc1f\xab\xf3\xa4^f\x8bD4f\x8bT8\xe8\"\x00\x81>\xfe}U\xaau\x85\x89\xecZ_\x07f\xb8!GPT\xfa\xff\xe4f!\xd2t\x04"
    b"f\x83\xc8\xfff\xab\xc3\xbb\x00|f`fRfP\x06Sj\x01j\x10\x89\xe6f\xf7v\xdc\xc0\xe4\x06\x88\xe1\x88\xc5\x92\xf6v\xe0\x88\xc6"
    b"\x08\xe1A\xb8\x01\x02\x8aV\x00\xcd\x13\x8dd\x10far\x0c\x02~\xfbf\x83\xc0\x01f\x83\xd2\x00\xc3\xe8\x0c\x00Disk error\r\n"
    b"^\xac\xb4\x0e\x8a>b\x04\xb3\x07\xcd\x10<\nu\xf1\xcd\xe7\xb5\x8d\x0bz\x0bE\x1c\xad\x8d \x01\xa3\x8f\x7f\xb7\xa8~C\xba\x00\x00"
)


@dataclass(frozen=True, slots=True)
class GptPartition:
    number: int
    name: str
    type_guid: uuid.UUID
    unique_guid: uuid.UUID
    start_lba: int
    end_lba: int
    flags: int

    @property
    def sector_count(self) -> int:
        return max(0, self.end_lba - self.start_lba + 1)

    @property
    def size_bytes(self) -> int:
        return self.sector_count * SECTOR_SIZE


@dataclass(frozen=True, slots=True)
class GptTable:
    disk_guid: uuid.UUID
    total_sectors: int
    first_usable_lba: int
    last_usable_lba: int
    partitions: tuple[GptPartition, ...]

    def get_by_name(self, name: str) -> GptPartition | None:
        target = name.upper()
        return next((p for p in self.partitions if p.name.upper() == target), None)

    def get_by_num(self, num: int) -> GptPartition | None:
        return next((p for p in self.partitions if p.number == num), None)

    @property
    def is_chromeos(self) -> bool:
        has_state = any(p.name.upper() == "STATE" and p.type_guid == CHROMEOS_STATE_TYPE for p in self.partitions)
        has_kern = any(p.name.upper().startswith("KERN") and p.type_guid == CHROMEOS_KERN_TYPE for p in self.partitions)
        has_root = any(p.name.upper().startswith("ROOT") and p.type_guid == CHROMEOS_ROOT_TYPE for p in self.partitions)
        has_efi = any(p.name.upper() == "EFI-SYSTEM" and p.type_guid == EFI_SYSTEM_TYPE for p in self.partitions)
        return has_state and has_kern and has_root and has_efi


@dataclass(frozen=True, slots=True)
class ChromeOSBootOptions:
    cros_debug: bool = False
    disable_auto_updates: bool = False
    verbose_boot: bool = False
    timeout: int = 2
    nomodeset: bool = False
    unencrypted_stateful: bool = False
    force_legacy_boot: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "cros_debug": self.cros_debug,
            "disable_auto_updates": self.disable_auto_updates,
            "verbose_boot": self.verbose_boot,
            "timeout": self.timeout,
            "nomodeset": self.nomodeset,
            "unencrypted_stateful": self.unencrypted_stateful,
            "force_legacy_boot": self.force_legacy_boot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ChromeOSBootOptions:
        return cls(
            cros_debug=bool(data.get("cros_debug")),
            disable_auto_updates=bool(data.get("disable_auto_updates")),
            verbose_boot=bool(data.get("verbose_boot")),
            timeout=int(data.get("timeout") or 2),
            nomodeset=bool(data.get("nomodeset")),
            unencrypted_stateful=bool(data.get("unencrypted_stateful")),
            force_legacy_boot=bool(data.get("force_legacy_boot")),
        )


def read_gpt_table(stream: BinaryIO) -> GptTable | None:
    stream.seek(SECTOR_SIZE)
    hdr_bytes = stream.read(SECTOR_SIZE)
    if len(hdr_bytes) < SECTOR_SIZE or hdr_bytes[:8] != b"EFI PART":
        return None

    try:
        (
            sig,
            rev,
            hdr_sz,
            hdr_crc,
            res,
            current_lba,
            backup_lba,
            first_usable,
            last_usable,
            disk_guid_raw,
            entry_lba,
            num_entries,
            entry_sz,
            entry_crc,
        ) = struct.unpack_from("<8sIIIIQQQQ16sQIII", hdr_bytes, 0)
    except struct.error:
        return None

    if hdr_sz < GPT_HEADER_SIZE or entry_sz != GPT_ENTRY_SIZE:
        return None

    stream.seek(entry_lba * SECTOR_SIZE)
    raw_entries = stream.read(num_entries * entry_sz)
    if len(raw_entries) < num_entries * entry_sz:
        return None

    disk_guid = uuid.UUID(bytes_le=disk_guid_raw)
    partitions: list[GptPartition] = []

    for idx in range(num_entries):
        off = idx * entry_sz
        t_guid, u_guid, s_lba, e_lba, flags, name_raw = struct.unpack_from("<16s16sQQQ72s", raw_entries, off)
        if t_guid == b"\x00" * 16 or s_lba == 0:
            continue
        name = name_raw.decode("utf-16le", errors="ignore").split("\x00", 1)[0].strip()
        partitions.append(
            GptPartition(
                number=idx + 1,
                name=name,
                type_guid=uuid.UUID(bytes_le=t_guid),
                unique_guid=uuid.UUID(bytes_le=u_guid),
                start_lba=s_lba,
                end_lba=e_lba,
                flags=flags,
            )
        )

    return GptTable(
        disk_guid=disk_guid,
        total_sectors=backup_lba + 1 if backup_lba > current_lba else current_lba + 1,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        partitions=tuple(partitions),
    )


def is_chromeos_drive(path_or_device: Path | str) -> bool:
    try:
        with open(path_or_device, "rb") as fh:
            table = read_gpt_table(fh)
            return table is not None and table.is_chromeos
    except (OSError, PermissionError):
        return False


def build_protective_mbr(
    total_sectors: int,
    boot_code: bytes | None = None,
    efi_lba: int = 299008,
    efi_sectors: int = 524288,
) -> bytes:
    mbr = bytearray(SECTOR_SIZE)
    code = boot_code if boot_code is not None else CHROMEOS_HYBRID_MBR_CODE
    mbr[: min(len(code), 446)] = code[:446]

    # Entry 1: Partition 12 (EFI-SYSTEM), active/bootable for Syslinux legacy boot
    mbr[446:462] = struct.pack(
        "<BBBBBBBBII",
        0x80,
        0, 0, 0,
        0x0C,
        0xFF, 0xFF, 0xFF,
        efi_lba,
        efi_sectors,
    )
    # Entry 2: GPT protective entry covering disk
    mbr[462:478] = struct.pack(
        "<BBBBBBBBII",
        0x00,
        0, 2, 0,
        0xEE,
        0xFF, 0xFF, 0xFF,
        1,
        1,
    )
    mbr[510:512] = b"\x55\xAA"
    return bytes(mbr)


def serialize_gpt(
    total_sectors: int,
    partitions: list[tuple[int, str, uuid.UUID, uuid.UUID, int, int, int]],
    disk_guid: uuid.UUID | None = None,
) -> tuple[bytes, bytes, bytes]:
    first_usable = 34
    last_usable = total_sectors - 34
    d_guid = disk_guid or uuid.uuid4()

    entries = bytearray(GPT_NUM_ENTRIES * GPT_ENTRY_SIZE)
    for p_num, p_name, t_guid, u_guid, s_lba, e_lba, flags in partitions:
        if 1 <= p_num <= GPT_NUM_ENTRIES:
            off = (p_num - 1) * GPT_ENTRY_SIZE
            name_encoded = p_name.encode("utf-16le").ljust(72, b"\x00")[:72]
            entries[off : off + GPT_ENTRY_SIZE] = struct.pack(
                "<16s16sQQQ72s",
                t_guid.bytes_le,
                u_guid.bytes_le,
                s_lba,
                e_lba,
                flags,
                name_encoded,
            )

    entry_crc = zlib.crc32(entries)

    primary_hdr = bytearray(SECTOR_SIZE)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        primary_hdr,
        0,
        b"EFI PART",
        0x00010000,
        GPT_HEADER_SIZE,
        0,
        0,
        1,
        total_sectors - 1,
        first_usable,
        last_usable,
        d_guid.bytes_le,
        2,
        GPT_NUM_ENTRIES,
        GPT_ENTRY_SIZE,
        entry_crc,
    )
    struct.pack_into("<I", primary_hdr, 16, zlib.crc32(primary_hdr[:GPT_HEADER_SIZE]))

    backup_hdr = bytearray(SECTOR_SIZE)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        backup_hdr,
        0,
        b"EFI PART",
        0x00010000,
        GPT_HEADER_SIZE,
        0,
        0,
        total_sectors - 1,
        1,
        first_usable,
        last_usable,
        d_guid.bytes_le,
        total_sectors - 33,
        GPT_NUM_ENTRIES,
        GPT_ENTRY_SIZE,
        entry_crc,
    )
    struct.pack_into("<I", backup_hdr, 16, zlib.crc32(backup_hdr[:GPT_HEADER_SIZE]))

    return bytes(primary_hdr), bytes(entries), bytes(backup_hdr)


def generate_chromeos_togo_partitions(
    total_sectors: int,
) -> list[tuple[int, str, uuid.UUID, uuid.UUID, int, int, int]]:
    last_usable = total_sectors - 34

    kern_a_flags = (15 << 48) | (15 << 52) | (1 << 56)
    kern_b_flags = 0

    state_start_lba = 6004736
    state_end_lba = last_usable

    return [
        (11, "RWFW", CHROMEOS_RWFW_TYPE, uuid.uuid4(), 64, 64, 0),
        (6, "KERN-C", CHROMEOS_KERN_TYPE, uuid.uuid4(), 65, 65, 0),
        (7, "ROOT-C", CHROMEOS_ROOT_TYPE, uuid.uuid4(), 66, 66, 0),
        (9, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), 67, 67, 0),
        (10, "reserved", CHROMEOS_RESERVED_TYPE, uuid.uuid4(), 68, 68, 0),
        (2, "KERN-A", CHROMEOS_KERN_TYPE, uuid.UUID("4e556b56-dc0f-0440-b653-0cd6a63da650"), 69, 131140, kern_a_flags),
        (4, "KERN-B", CHROMEOS_KERN_TYPE, uuid.UUID("5c9d2135-4510-1440-8863-a85e63f52328"), 131141, 262212, kern_b_flags),
        (8, "OEM", CHROMEOS_STATE_TYPE, uuid.UUID("c19a8d91-cad4-114f-9e93-925e7bd55c45"), 266240, 299007, 0),
        (12, "EFI-SYSTEM", EFI_SYSTEM_TYPE, uuid.UUID("0b8db5e7-0b7a-1c45-ad8d-2001a38f7fb7"), 299008, 823295, 4),
        (5, "ROOT-B", CHROMEOS_ROOT_TYPE, ROOT_B_UNIQUE_UUID, 823296, 827391, 0),
        (3, "ROOT-A", CHROMEOS_ROOT_TYPE, ROOT_A_UNIQUE_UUID, 827392, 5742591, 0),
        (1, "STATE", CHROMEOS_STATE_TYPE, uuid.UUID("2d27eb8c-0505-7b43-9472-aec55afbf3f7"), state_start_lba, state_end_lba, 0),
    ]


def patch_grub_cfg(
    content: str,
    options: ChromeOSBootOptions,
) -> str:
    lines = content.splitlines()
    out: list[str] = []
    for line in lines:
        if line.strip().startswith("set timeout="):
            out.append(f"set timeout={options.timeout}")
            continue

        if "linux " in line and "vmlinuz" in line:
            parts = line.split()
            # cros_debug
            if options.cros_debug and "cros_debug" not in parts:
                idx = parts.index("cros_efi") if "cros_efi" in parts else len(parts)
                parts.insert(idx + 1, "cros_debug")
            elif not options.cros_debug and "cros_debug" in parts:
                parts = [p for p in parts if p != "cros_debug"]

            # verbose boot: remove quiet, add console=tty1 loglevel=7
            if options.verbose_boot:
                parts = [p for p in parts if p != "quiet"]
                if "console=tty1" not in parts:
                    parts.append("console=tty1")
            else:
                if "quiet" not in parts:
                    parts.insert(2, "quiet")
                parts = [p for p in parts if p != "console=tty1"]

            # nomodeset
            if options.nomodeset:
                if "nomodeset" not in parts:
                    parts.append("nomodeset")
                parts = [p for p in parts if p != "i915.modeset=1"]
            else:
                parts = [p for p in parts if p != "nomodeset"]
                if "i915.modeset=1" not in parts:
                    parts.append("i915.modeset=1")

            out.append(" ".join(parts))
            continue

        out.append(line)

    return "\n".join(out) + "\n"


def parse_grub_cfg_options(cfg_text: str) -> ChromeOSBootOptions:
    cros_debug = "cros_debug" in cfg_text
    verbose_boot = "quiet" not in cfg_text or "console=tty1" in cfg_text
    nomodeset = "nomodeset" in cfg_text or "i915.modeset=0" in cfg_text
    m = re.search(r"set timeout=(\d+)", cfg_text)
    timeout = int(m.group(1)) if m else 2
    return ChromeOSBootOptions(
        cros_debug=cros_debug,
        verbose_boot=verbose_boot,
        timeout=timeout,
        nomodeset=nomodeset,
    )


def read_chromeos_boot_options(path_or_device: Path | str) -> ChromeOSBootOptions | None:
    try:
        with open(path_or_device, "rb") as fh:
            table = read_gpt_table(fh)
            if table is None or not table.is_chromeos:
                return None

            efi_part = table.get_by_name("EFI-SYSTEM")
            if efi_part is None:
                return None

            fh.seek(efi_part.start_lba * SECTOR_SIZE)
            # Read first 16 MiB of EFI-SYSTEM where grub.cfg resides
            efi_chunk = fh.read(min(efi_part.size_bytes, 16 * 1024 * 1024))
            idx = efi_chunk.find(b"menuentry \"local image A\"")
            if idx == -1:
                idx = efi_chunk.find(b"menuentry \"verified image A\"")
            if idx == -1:
                return ChromeOSBootOptions()

            start_cfg = max(0, idx - 400)
            end_cfg = min(len(efi_chunk), idx + 2500)
            cfg_text = efi_chunk[start_cfg:end_cfg].decode("utf-8", errors="ignore")
            opts = parse_grub_cfg_options(cfg_text)

            # Check STATE partition for flags
            state_part = table.get_by_name("STATE")
            disable_au = False
            unenc = False
            if state_part is not None:
                fh.seek(state_part.start_lba * SECTOR_SIZE)
                state_chunk = fh.read(min(state_part.size_bytes, 32 * 1024 * 1024))
                if b"consumer-auto-update-disabled" in state_chunk:
                    disable_au = True
                if b".force_unencrypted_stateful" in state_chunk or b"encstateful_mount_option" in state_chunk:
                    unenc = True

            is_legacy_forced = (efi_chunk.find(b"BOOTX64 DIS") != -1 or efi_chunk.find(b"BOOTX64.DIS") != -1)

            return ChromeOSBootOptions(
                cros_debug=opts.cros_debug,
                disable_auto_updates=disable_au,
                verbose_boot=opts.verbose_boot,
                timeout=opts.timeout,
                nomodeset=opts.nomodeset,
                unencrypted_stateful=unenc,
                force_legacy_boot=is_legacy_forced,
            )
    except (OSError, PermissionError):
        return None


def patch_grub_in_block_device(
    path_or_device: Path | str | int,
    options: ChromeOSBootOptions,
) -> bool:
    close_when_done = False
    if isinstance(path_or_device, int):
        fd = path_or_device
    else:
        try:
            fd = os.open(str(path_or_device), os.O_RDWR)
            close_when_done = True
        except (OSError, PermissionError):
            return False

    try:
        header_buf = os.pread(fd, SECTOR_SIZE, SECTOR_SIZE)
        if len(header_buf) < SECTOR_SIZE or header_buf[:8] != b"EFI PART":
            return False

        efi_lba = 299008
        part_entries_lba = struct.unpack_from("<Q", header_buf, 72)[0]
        num_entries = struct.unpack_from("<I", header_buf, 80)[0]
        entry_size = struct.unpack_from("<I", header_buf, 84)[0]

        entries_buf = os.pread(fd, num_entries * entry_size, part_entries_lba * SECTOR_SIZE)
        for i in range(num_entries):
            off = i * entry_size
            name_raw = entries_buf[off + 56 : off + 56 + 72]
            name = name_raw.decode("utf-16le", errors="ignore").split("\x00", 1)[0]
            if name.upper() == "EFI-SYSTEM":
                efi_lba = struct.unpack_from("<Q", entries_buf, off + 32)[0]
                break

        efi_base_bytes = efi_lba * SECTOR_SIZE
        chunk = os.pread(fd, 2 * 1024 * 1024, efi_base_bytes)
        idx = chunk.find(b"menuentry \"local image A\"")
        if idx == -1:
            idx = chunk.find(b"menuentry \"verified image A\"")
        if idx == -1:
            return False

        start_idx = chunk.rfind(b"defaultA=", 0, idx)
        if start_idx == -1:
            start_idx = chunk.rfind(b"gptpriority", 0, idx)
        if start_idx == -1:
            start_idx = max(0, idx - 300)

        alt_idx = chunk.find(b"Alternate USB Boot", idx)
        end_idx = chunk.find(b"}", alt_idx if alt_idx != -1 else idx)
        if end_idx == -1:
            end_idx = min(len(chunk), idx + 2000)
        else:
            end_idx += 1

        original_text = chunk[start_idx:end_idx].decode("utf-8", errors="ignore")
        patched_text = patch_grub_cfg(original_text, options)
        patched_bytes = patched_text.encode("utf-8")

        fat_dir_idx = chunk.find(b"GRUB    CFG")
        if fat_dir_idx != -1 and (fat_dir_idx + 32) <= len(chunk):
            size_field_off = efi_base_bytes + fat_dir_idx + 28
            os.pwrite(fd, struct.pack("<I", len(patched_bytes)), size_field_off)

        write_off = efi_base_bytes + start_idx
        orig_len = end_idx - start_idx
        if len(patched_bytes) < orig_len:
            patched_bytes = patched_bytes + (b" " * (orig_len - len(patched_bytes)))
        os.pwrite(fd, patched_bytes, write_off)

        # Force legacy boot toggle: disable or re-enable BOOTX64.EFI and BOOTIA32.EFI
        bootx64_idx = chunk.find(b"BOOTX64 EFI")
        bootx64_dis_idx = chunk.find(b"BOOTX64 DIS")
        if options.force_legacy_boot:
            if bootx64_idx != -1:
                os.pwrite(fd, b"DIS", efi_base_bytes + bootx64_idx + 8)
                lfn_ext = chunk.rfind(b"e\x00f\x00i\x00", max(0, bootx64_idx - 64), bootx64_idx)
                if lfn_ext != -1:
                    os.pwrite(fd, b"d\x00i\x00s\x00", efi_base_bytes + lfn_ext)
            boot32_idx = chunk.find(b"BOOTIA32EFI")
            if boot32_idx != -1:
                os.pwrite(fd, b"DIS", efi_base_bytes + boot32_idx + 8)
                lfn32 = chunk.rfind(b"e\x00f\x00i\x00", max(0, boot32_idx - 64), boot32_idx)
                if lfn32 != -1:
                    os.pwrite(fd, b"d\x00i\x00s\x00", efi_base_bytes + lfn32)
        else:
            if bootx64_dis_idx != -1:
                os.pwrite(fd, b"EFI", efi_base_bytes + bootx64_dis_idx + 8)
                lfn_dis = chunk.rfind(b"d\x00i\x00s\x00", max(0, bootx64_dis_idx - 64), bootx64_dis_idx)
                if lfn_dis != -1:
                    os.pwrite(fd, b"e\x00f\x00i\x00", efi_base_bytes + lfn_dis)
            boot32_dis_idx = chunk.find(b"BOOTIA32DIS")
            if boot32_dis_idx != -1:
                os.pwrite(fd, b"EFI", efi_base_bytes + boot32_dis_idx + 8)
                lfn32_dis = chunk.rfind(b"d\x00i\x00s\x00", max(0, boot32_dis_idx - 64), boot32_dis_idx)
                if lfn32_dis != -1:
                    os.pwrite(fd, b"e\x00f\x00i\x00", efi_base_bytes + lfn32_dis)

        os.fsync(fd)
        return True
    except (OSError, PermissionError):
        return False
    finally:
        if close_when_done:
            os.close(fd)

