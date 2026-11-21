from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import struct
from typing import BinaryIO
import uuid
import zlib

SECTOR_SIZE = 512
GPT_HEADER_SIZE = 92
GPT_NUM_ENTRIES = 128
GPT_ENTRY_SIZE = 128

TWO_MIB_SECTORS = (2 * 1024 * 1024) // SECTOR_SIZE
ROOTFS_MIN_SECTORS = (4 * 1024 * 1024 * 1024) // SECTOR_SIZE
STATE_GAP_SECTORS = (128 * 1024 * 1024) // SECTOR_SIZE
TAIL_RESERVED_SECTORS = 24576 // SECTOR_SIZE
MIN_TOGO_STATE_SECTORS = (2 * 1024 * 1024 * 1024) // SECTOR_SIZE
MIN_TOGO_ROOT_SECTORS = (2 * 1024 * 1024 * 1024) // SECTOR_SIZE

CHROMEOS_KERN_TYPE = uuid.UUID("fe3a2a5d-4f32-41a7-b725-accc3285a309")
CHROMEOS_ROOT_TYPE = uuid.UUID("3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec")
CHROMEOS_STATE_TYPE = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
EFI_SYSTEM_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
CHROMEOS_RWFW_TYPE = uuid.UUID("cab6e88e-abf3-4102-a07a-d4bb9be3c1d3")
CHROMEOS_RESERVED_TYPE = uuid.UUID("2e0a753d-9e48-43b0-8337-b15192cb1b5e")

KERN_A_INSTALL_FLAGS = (15 << 48) | (15 << 52)
KERN_INACTIVE_FLAGS = 15 << 52
EFI_LEGACY_BOOT_FLAG = 1 << 2

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

    @property
    def is_chromeos_togo(self) -> bool:
        if not self.is_chromeos:
            return False
        root_a = self.get_by_name("ROOT-A")
        root_b = self.get_by_name("ROOT-B")
        kern_a = self.get_by_name("KERN-A")
        kern_b = self.get_by_name("KERN-B")
        state = self.get_by_name("STATE")
        efi = self.get_by_name("EFI-SYSTEM")
        if root_a is None or root_b is None or kern_a is None or kern_b is None or state is None or efi is None:
            return False
        # ChromeOS is_running_from_installer treats equal ROOT-A/ROOT-B sizes as an installed disk.
        return (
            root_a.sector_count == root_b.sector_count
            and root_a.sector_count >= MIN_TOGO_ROOT_SECTORS
            and state.start_lba > max(root_a.end_lba, root_b.end_lba)
        )


def _unpack_gpt_header(hdr_bytes: bytes) -> tuple[int, int, int, int, int, uuid.UUID, int, int, int] | None:
    if len(hdr_bytes) < SECTOR_SIZE or hdr_bytes[:8] != b"EFI PART":
        return None
    try:
        (
            _sig,
            _rev,
            hdr_sz,
            _hdr_crc,
            _res,
            current_lba,
            backup_lba,
            first_usable,
            last_usable,
            disk_guid_raw,
            entry_lba,
            num_entries,
            entry_sz,
            _entry_crc,
        ) = struct.unpack_from("<8sIIIIQQQQ16sQIII", hdr_bytes, 0)
    except struct.error:
        return None
    if hdr_sz < GPT_HEADER_SIZE or entry_sz != GPT_ENTRY_SIZE:
        return None
    return (
        current_lba,
        backup_lba,
        first_usable,
        last_usable,
        entry_lba,
        uuid.UUID(bytes_le=disk_guid_raw),
        num_entries,
        entry_sz,
        hdr_sz,
    )


def _parse_gpt_table(
    current_lba: int,
    backup_lba: int,
    first_usable: int,
    last_usable: int,
    disk_guid: uuid.UUID,
    num_entries: int,
    entry_sz: int,
    raw_entries: bytes,
) -> GptTable:
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


def read_gpt_table_from_bytes(header_and_entries: bytes) -> GptTable | None:
    if len(header_and_entries) < 2 * SECTOR_SIZE:
        return None
    hdr = _unpack_gpt_header(header_and_entries[SECTOR_SIZE : 2 * SECTOR_SIZE])
    if hdr is None:
        return None
    current_lba, backup_lba, first_usable, last_usable, entry_lba, disk_guid, num_entries, entry_sz, _ = hdr
    entries_off = entry_lba * SECTOR_SIZE
    entries_len = num_entries * entry_sz
    if entries_off + entries_len > len(header_and_entries):
        return None
    raw_entries = header_and_entries[entries_off : entries_off + entries_len]
    return _parse_gpt_table(current_lba, backup_lba, first_usable, last_usable, disk_guid, num_entries, entry_sz, raw_entries)


def read_gpt_table(stream: BinaryIO) -> GptTable | None:
    stream.seek(SECTOR_SIZE)
    hdr_bytes = stream.read(SECTOR_SIZE)
    hdr = _unpack_gpt_header(hdr_bytes)
    if hdr is None:
        return None
    current_lba, backup_lba, first_usable, last_usable, entry_lba, disk_guid, num_entries, entry_sz, _ = hdr
    stream.seek(entry_lba * SECTOR_SIZE)
    raw_entries = stream.read(num_entries * entry_sz)
    if len(raw_entries) < num_entries * entry_sz:
        return None
    return _parse_gpt_table(current_lba, backup_lba, first_usable, last_usable, disk_guid, num_entries, entry_sz, raw_entries)


def is_chromeos_drive(path_or_device: Path | str) -> bool:
    try:
        with open(path_or_device, "rb") as fh:
            table = read_gpt_table(fh)
            return table is not None and table.is_chromeos
    except (OSError, PermissionError):
        return False


def is_chromeos_togo_drive(path_or_device: Path | str) -> bool:
    try:
        with open(path_or_device, "rb") as fh:
            table = read_gpt_table(fh)
            return table is not None and table.is_chromeos_togo
    except (OSError, PermissionError):
        return False


def build_protective_mbr(
    total_sectors: int,
    boot_code: bytes | None = None,
    efi_lba: int = 299008,
    efi_sectors: int = 524288,
) -> bytes:
    _ = total_sectors
    mbr = bytearray(SECTOR_SIZE)
    code = boot_code if boot_code is not None else CHROMEOS_HYBRID_MBR_CODE
    mbr[: min(len(code), 446)] = code[:446]

    # Entry 1: Partition 12 (EFI-SYSTEM), active/bootable for legacy Syslinux boot.
    mbr[446:462] = struct.pack(
        "<BBBBBBBBII",
        0x80,
        0,
        0,
        0,
        0x0C,
        0xFF,
        0xFF,
        0xFF,
        efi_lba,
        efi_sectors,
    )
    # Entry 2: GPT protective entry.
    mbr[462:478] = struct.pack(
        "<BBBBBBBBII",
        0x00,
        0,
        2,
        0,
        0xEE,
        0xFF,
        0xFF,
        0xFF,
        1,
        1,
    )
    mbr[510:512] = b"\x55\xAA"
    return bytes(mbr)


def serialize_gpt(
    total_sectors: int,
    partitions: Sequence[GptPartition | tuple[int, str, uuid.UUID, uuid.UUID, int, int, int]],
    disk_guid: uuid.UUID | None = None,
) -> tuple[bytes, bytes, bytes]:
    first_usable = 34
    last_usable = total_sectors - 34
    d_guid = disk_guid or uuid.uuid4()

    entries = bytearray(GPT_NUM_ENTRIES * GPT_ENTRY_SIZE)
    for part in partitions:
        if isinstance(part, GptPartition):
            p_num = part.number
            p_name = part.name
            t_guid = part.type_guid
            u_guid = part.unique_guid
            s_lba = part.start_lba
            e_lba = part.end_lba
            flags = part.flags
        else:
            p_num, p_name, t_guid, u_guid, s_lba, e_lba, flags = part

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


def _align_up(value: int, alignment: int) -> int:
    rem = value % alignment
    return value if rem == 0 else value + (alignment - rem)


def plan_chromeos_togo_layout(source_gpt: GptTable, total_sectors: int) -> list[GptPartition]:
    if not source_gpt.is_chromeos:
        raise ValueError("Source image does not contain a valid ChromeOS GPT layout.")

    src_kern_a = source_gpt.get_by_name("KERN-A")
    src_kern_b = source_gpt.get_by_name("KERN-B")
    src_oem = source_gpt.get_by_name("OEM")
    src_efi = source_gpt.get_by_name("EFI-SYSTEM")
    src_root_a = source_gpt.get_by_name("ROOT-A")
    src_root_b = source_gpt.get_by_name("ROOT-B")
    src_state = source_gpt.get_by_name("STATE")

    if src_kern_a is None or src_kern_b is None or src_oem is None or src_efi is None or src_root_a is None:
        raise ValueError("Source image is missing required ChromeOS partitions.")

    src_rwfw = source_gpt.get_by_num(11)
    src_kern_c = source_gpt.get_by_num(6)
    src_root_c = source_gpt.get_by_num(7)
    src_res9 = source_gpt.get_by_num(9)
    src_res10 = source_gpt.get_by_num(10)

    curr = 64
    planned: list[GptPartition] = []

    def add_seq(
        num: int,
        name: str,
        type_guid: uuid.UUID,
        unique_guid: uuid.UUID,
        sectors: int,
        flags: int = 0,
    ) -> None:
        nonlocal curr
        planned.append(
            GptPartition(
                number=num,
                name=name,
                type_guid=type_guid,
                unique_guid=unique_guid,
                start_lba=curr,
                end_lba=curr + sectors - 1,
                flags=flags,
            )
        )
        curr += sectors

    add_seq(
        11,
        "RWFW",
        CHROMEOS_RWFW_TYPE,
        src_rwfw.unique_guid if src_rwfw else uuid.uuid4(),
        src_rwfw.sector_count if src_rwfw and src_rwfw.sector_count > 0 else 1,
    )
    add_seq(
        6,
        "KERN-C",
        CHROMEOS_KERN_TYPE,
        src_kern_c.unique_guid if src_kern_c else uuid.uuid4(),
        src_kern_c.sector_count if src_kern_c and src_kern_c.sector_count > 0 else 1,
        KERN_INACTIVE_FLAGS,
    )
    add_seq(
        7,
        "ROOT-C",
        CHROMEOS_ROOT_TYPE,
        src_root_c.unique_guid if src_root_c else uuid.uuid4(),
        src_root_c.sector_count if src_root_c and src_root_c.sector_count > 0 else 1,
    )
    add_seq(
        9,
        "reserved",
        CHROMEOS_RESERVED_TYPE,
        src_res9.unique_guid if src_res9 else uuid.uuid4(),
        src_res9.sector_count if src_res9 and src_res9.sector_count > 0 else 1,
    )
    add_seq(
        10,
        "reserved",
        CHROMEOS_RESERVED_TYPE,
        src_res10.unique_guid if src_res10 else uuid.uuid4(),
        src_res10.sector_count if src_res10 and src_res10.sector_count > 0 else 1,
    )

    kern_a_secs = src_kern_a.sector_count
    kern_b_secs = max(src_kern_b.sector_count, kern_a_secs)
    add_seq(2, "KERN-A", CHROMEOS_KERN_TYPE, src_kern_a.unique_guid, kern_a_secs, KERN_A_INSTALL_FLAGS)
    add_seq(4, "KERN-B", CHROMEOS_KERN_TYPE, src_kern_b.unique_guid, kern_b_secs, KERN_INACTIVE_FLAGS)

    curr = _align_up(curr, TWO_MIB_SECTORS)
    add_seq(8, "OEM", CHROMEOS_STATE_TYPE, src_oem.unique_guid, src_oem.sector_count)
    add_seq(12, "EFI-SYSTEM", EFI_SYSTEM_TYPE, src_efi.unique_guid, src_efi.sector_count, EFI_LEGACY_BOOT_FLAG)

    curr = _align_up(curr, TWO_MIB_SECTORS)
    rootfs_sectors = max(ROOTFS_MIN_SECTORS, _align_up(src_root_a.sector_count, TWO_MIB_SECTORS))
    root_b_guid = src_root_b.unique_guid if src_root_b is not None else uuid.uuid4()
    add_seq(5, "ROOT-B", CHROMEOS_ROOT_TYPE, root_b_guid, rootfs_sectors)

    curr = _align_up(curr, TWO_MIB_SECTORS)
    add_seq(3, "ROOT-A", CHROMEOS_ROOT_TYPE, src_root_a.unique_guid, rootfs_sectors)

    curr = _align_up(curr + STATE_GAP_SECTORS, TWO_MIB_SECTORS)
    state_start_lba = curr
    state_end_lba = total_sectors - 1 - TAIL_RESERVED_SECTORS
    state_sectors = state_end_lba - state_start_lba + 1
    if state_end_lba > total_sectors - 34 or state_sectors < MIN_TOGO_STATE_SECTORS:
        raise ValueError(
            "Target drive is too small for ChromeOS To Go (requires at least 2 GiB free for user data after OS partitions)."
        )

    planned.append(
        GptPartition(
            number=1,
            name="STATE",
            type_guid=CHROMEOS_STATE_TYPE,
            unique_guid=src_state.unique_guid if src_state is not None else uuid.uuid4(),
            start_lba=state_start_lba,
            end_lba=state_end_lba,
            flags=0,
        )
    )
    return planned


def plan_chromeos_togo_update(source_gpt: GptTable, target_gpt: GptTable) -> list[GptPartition]:
    if not source_gpt.is_chromeos:
        raise ValueError("Source image does not contain a valid ChromeOS GPT layout.")
    if not target_gpt.is_chromeos_togo:
        raise ValueError("Target drive does not have a valid ChromeOS To Go installation to update.")

    src_kern = source_gpt.get_by_name("KERN-B") or source_gpt.get_by_name("KERN-A")
    src_kern_a = source_gpt.get_by_name("KERN-A")
    src_kern_b = source_gpt.get_by_name("KERN-B")
    src_oem = source_gpt.get_by_name("OEM")
    src_efi = source_gpt.get_by_name("EFI-SYSTEM")
    src_root_a = source_gpt.get_by_name("ROOT-A")
    src_root_b = source_gpt.get_by_name("ROOT-B")

    dst_kern_a = target_gpt.get_by_name("KERN-A")
    dst_kern_b = target_gpt.get_by_name("KERN-B")
    dst_oem = target_gpt.get_by_name("OEM")
    dst_efi = target_gpt.get_by_name("EFI-SYSTEM")
    dst_root_a = target_gpt.get_by_name("ROOT-A")
    dst_root_b = target_gpt.get_by_name("ROOT-B")
    dst_state = target_gpt.get_by_name("STATE")

    if (
        src_kern is None
        or src_kern_a is None
        or src_kern_b is None
        or src_oem is None
        or src_efi is None
        or src_root_a is None
        or dst_kern_a is None
        or dst_kern_b is None
        or dst_oem is None
        or dst_efi is None
        or dst_root_a is None
        or dst_root_b is None
        or dst_state is None
    ):
        raise ValueError("Missing required ChromeOS partitions for To Go update.")

    if dst_kern_a.sector_count < src_kern.sector_count or dst_kern_b.sector_count < src_kern.sector_count:
        raise ValueError("Target KERN partitions are smaller than the source image.")
    if dst_oem.sector_count < src_oem.sector_count:
        raise ValueError("Target OEM partition is smaller than the source image.")
    if dst_efi.sector_count < src_efi.sector_count:
        raise ValueError("Target EFI-SYSTEM partition is smaller than the source image.")
    if dst_root_a.sector_count < src_root_a.sector_count or dst_root_b.sector_count < src_root_a.sector_count:
        raise ValueError("Target ROOT partitions are smaller than the source image.")

    os_parts = [p for p in target_gpt.partitions if p.number != dst_state.number]
    if any(p.end_lba >= dst_state.start_lba for p in os_parts):
        raise ValueError("Target OS partitions overlap or follow STATE; refusing to update in place.")

    updated: list[GptPartition] = []
    for part in target_gpt.partitions:
        upper = part.name.upper()
        if upper == "STATE":
            updated.append(part)
        elif upper == "KERN-A":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=src_kern_a.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=KERN_A_INSTALL_FLAGS,
                )
            )
        elif upper == "KERN-B":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=src_kern_b.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=KERN_INACTIVE_FLAGS,
                )
            )
        elif upper == "KERN-C":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=part.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=KERN_INACTIVE_FLAGS,
                )
            )
        elif upper == "ROOT-A":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=src_root_a.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=0,
                )
            )
        elif upper == "ROOT-B":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=src_root_b.unique_guid if src_root_b is not None else part.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=0,
                )
            )
        elif upper == "EFI-SYSTEM":
            updated.append(
                GptPartition(
                    number=part.number,
                    name=part.name,
                    type_guid=part.type_guid,
                    unique_guid=src_efi.unique_guid,
                    start_lba=part.start_lba,
                    end_lba=part.end_lba,
                    flags=EFI_LEGACY_BOOT_FLAG,
                )
            )
        else:
            updated.append(part)

    return updated
