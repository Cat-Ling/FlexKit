from __future__ import annotations

from collections.abc import Callable
import os
import struct
import time
import uuid

BLOCK_SIZE = 4096
INODE_SIZE = 256
DESC_SIZE = 64
BLOCKS_PER_GROUP = 32768
INODES_PER_GROUP = 8192
INODE_TABLE_BLOCKS = (INODES_PER_GROUP * INODE_SIZE) // BLOCK_SIZE

# Features matching ChromeOS `mkfs.ext4 -F -b 4096 -L H-STATE -O encrypt,verity` (without flex_bg/resize_inode).
EXT4_COMPAT = 0x002C  # HAS_JOURNAL | EXT_ATTR | DIR_INDEX
EXT4_INCOMPAT = 0x100C2  # FILETYPE | EXTENTS | 64BIT | ENCRYPT
EXT4_RO_COMPAT = 0x846B  # SPARSE_SUPER | LARGE_FILE | HUGE_FILE | DIR_NLINK | EXTRA_ISIZE | METADATA_CSUM | VERITY

EXT4_BG_INODE_UNINIT = 0x0001
EXT4_EXTENTS_FL = 0x00080000
EXT4_EXTENT_MAGIC = 0xF30A

JBD2_MAGIC = 0xC03B3998
JBD2_SUPERBLOCK_V2 = 4
JBD2_FEATURE_INCOMPAT_CSUM_V3 = 0x10
JBD2_CRC32C_CHKSUM = 4

_CRC32C_POLY = 0x82F63B78


def _build_crc32c_table() -> tuple[int, ...]:
    table: list[int] = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ _CRC32C_POLY if (crc & 1) else (crc >> 1)
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _build_crc32c_table()


def crc32c_raw(crc: int, data: bytes | bytearray | memoryview) -> int:
    table = _CRC32C_TABLE
    c = crc & 0xFFFFFFFF
    for b in data:
        c = table[(c ^ b) & 0xFF] ^ (c >> 8)
    return c


def _is_sparse_super_group(group: int) -> bool:
    if group <= 1:
        return True
    for base in (3, 5, 7):
        val = base
        while val < group:
            val *= base
        if val == group:
            return True
    return False


def _set_bitmap_range(bitmap: bytearray, start_bit: int, count: int) -> None:
    for bit in range(start_bit, start_bit + count):
        bitmap[bit >> 3] |= 1 << (bit & 7)


def _pack_inode(
    *,
    ino: int,
    fs_seed: int,
    mode: int,
    size_bytes: int,
    links_count: int,
    blocks_512: int,
    timestamp: int,
    extent_start_block: int,
    extent_block_count: int,
) -> bytes:
    buf = bytearray(INODE_SIZE)
    struct.pack_into("<HHIII", buf, 0x00, mode, 0, size_bytes & 0xFFFFFFFF, timestamp, timestamp)
    struct.pack_into("<IIHH", buf, 0x10, timestamp, 0, 0, links_count)
    struct.pack_into("<III", buf, 0x1C, blocks_512 & 0xFFFFFFFF, EXT4_EXTENTS_FL, 0)

    # Single-leaf extent tree inside i_block (60 bytes).
    struct.pack_into("<HHHHII", buf, 0x28, EXT4_EXTENT_MAGIC, 1, 4, 0, 0, 0)
    struct.pack_into(
        "<IHHI",
        buf,
        0x34,
        0,
        extent_block_count,
        (extent_start_block >> 32) & 0xFFFF,
        extent_start_block & 0xFFFFFFFF,
    )

    struct.pack_into("<I", buf, 0x6C, (size_bytes >> 32) & 0xFFFFFFFF)
    struct.pack_into("<H", buf, 0x76, (blocks_512 >> 32) & 0xFFFF)
    struct.pack_into("<H", buf, 0x80, 32)  # i_extra_isize

    ino_seed = crc32c_raw(fs_seed, struct.pack("<II", ino, 0))
    csum = crc32c_raw(ino_seed, buf)
    struct.pack_into("<H", buf, 0x7C, csum & 0xFFFF)
    struct.pack_into("<H", buf, 0x82, (csum >> 16) & 0xFFFF)
    return bytes(buf)


def _build_dir_block(
    *,
    dir_ino: int,
    parent_ino: int,
    fs_seed: int,
    extra_entries: tuple[tuple[int, str, int], ...] = (),
) -> bytes:
    blk = bytearray(BLOCK_SIZE)
    tail_off = BLOCK_SIZE - 12

    entries: list[tuple[int, bytes, int]] = [
        (dir_ino, b".", 2),
        (parent_ino, b"..", 2),
    ]
    for child_ino, child_name, ftype in extra_entries:
        entries.append((child_ino, child_name.encode("utf-8"), ftype))

    pos = 0
    for idx, (ino, name_bytes, ftype) in enumerate(entries):
        rec_len = (8 + len(name_bytes) + 3) & ~3
        if idx == len(entries) - 1:
            rec_len = tail_off - pos
        struct.pack_into("<IHBB", blk, pos, ino, rec_len, len(name_bytes), ftype)
        blk[pos + 8 : pos + 8 + len(name_bytes)] = name_bytes
        pos += rec_len

    ino_seed = crc32c_raw(fs_seed, struct.pack("<II", dir_ino, 0))
    csum = crc32c_raw(ino_seed, memoryview(blk)[:tail_off])
    struct.pack_into("<IHBB I", blk, tail_off, 0, 12, 0, 0xDE, csum)
    return bytes(blk)


def _build_journal_superblock(fs_uuid: uuid.UUID, journal_blocks: int) -> bytes:
    blk = bytearray(BLOCK_SIZE)
    struct.pack_into(
        ">12I",
        blk,
        0,
        JBD2_MAGIC,
        JBD2_SUPERBLOCK_V2,
        0,  # h_sequence
        BLOCK_SIZE,  # s_blocksize
        journal_blocks,  # s_maxlen
        1,  # s_first
        1,  # s_sequence
        0,  # s_start
        0,  # s_errno
        0,  # s_feature_compat
        JBD2_FEATURE_INCOMPAT_CSUM_V3,  # s_feature_incompat
        0,  # s_feature_ro_compat
    )
    blk[0x30:0x40] = fs_uuid.bytes
    struct.pack_into(">I", blk, 0x40, 1)  # s_nr_users
    blk[0x50] = JBD2_CRC32C_CHKSUM  # s_checksum_type
    csum = crc32c_raw(0xFFFFFFFF, memoryview(blk)[:1024])
    struct.pack_into(">I", blk, 0xFC, csum)
    return bytes(blk)


def format_ext4_partition(
    write_at: Callable[[int, bytes | memoryview], None],
    partition_offset: int,
    partition_size: int,
    *,
    label: str = "H-STATE",
    fs_uuid: uuid.UUID | None = None,
    timestamp: int | None = None,
) -> None:
    """Format an ext4 filesystem matching ChromeOS H-STATE directly over a byte-offset writer."""
    if partition_size < 64 * 1024 * 1024:
        raise ValueError(f"Partition size {partition_size} bytes is too small for ext4 H-STATE.")

    # Clear leading and trailing superblock regions before writing new metadata.
    zero_1m = b"\x00" * (1024 * 1024)
    head_wipe = min(2 * 1024 * 1024, partition_size)
    pos = 0
    while pos < head_wipe:
        chunk_len = min(len(zero_1m), head_wipe - pos)
        write_at(partition_offset + pos, memoryview(zero_1m)[:chunk_len])
        pos += chunk_len
    tail_wipe = min(1024 * 1024, partition_size - head_wipe)
    if tail_wipe > 0:
        write_at(partition_offset + partition_size - tail_wipe, memoryview(zero_1m)[:tail_wipe])

    total_blocks = partition_size // BLOCK_SIZE
    num_groups = (total_blocks + BLOCKS_PER_GROUP - 1) // BLOCKS_PER_GROUP

    while num_groups > 1:
        gdt_blocks = ((num_groups * DESC_SIZE) + BLOCK_SIZE - 1) // BLOCK_SIZE
        last_group_blocks = total_blocks - (num_groups - 1) * BLOCKS_PER_GROUP
        last_overhead = (1 + gdt_blocks if _is_sparse_super_group(num_groups - 1) else 0) + 2 + INODE_TABLE_BLOCKS
        if last_group_blocks < last_overhead + 16:
            total_blocks -= last_group_blocks
            num_groups -= 1
        else:
            break

    gdt_blocks = ((num_groups * DESC_SIZE) + BLOCK_SIZE - 1) // BLOCK_SIZE
    sparse_groups = [g for g in range(num_groups) if _is_sparse_super_group(g)]

    if total_blocks >= 524288:
        journal_blocks = 8192
    elif total_blocks >= 65536:
        journal_blocks = 4096
    else:
        journal_blocks = 1024

    now = int(time.time()) if timestamp is None else int(timestamp)
    uid = fs_uuid or uuid.uuid4()
    fs_seed = crc32c_raw(0xFFFFFFFF, uid.bytes)
    hash_seed = os.urandom(16)

    g0_overhead = 1 + gdt_blocks + 2 + INODE_TABLE_BLOCKS
    root_dir_blk = g0_overhead
    lost_found_blk = root_dir_blk + 1
    journal_start_blk = lost_found_blk + 1
    if journal_start_blk + journal_blocks >= min(total_blocks, BLOCKS_PER_GROUP):
        raise ValueError("Group 0 does not have enough contiguous space for ext4 metadata and journal.")

    gdt_buf = bytearray(gdt_blocks * BLOCK_SIZE)
    total_free_blocks = 0
    total_overhead_blocks = 0

    for g in range(num_groups):
        g_start = g * BLOCKS_PER_GROUP
        g_blocks = min(BLOCKS_PER_GROUP, total_blocks - g_start)
        has_sb = _is_sparse_super_group(g)
        sb_gdt_blocks = (1 + gdt_blocks) if has_sb else 0
        bb_blk = g_start + sb_gdt_blocks
        ib_blk = bb_blk + 1
        it_blk = ib_blk + 1
        meta_blocks = sb_gdt_blocks + 2 + INODE_TABLE_BLOCKS

        bmap = bytearray(BLOCK_SIZE)
        _set_bitmap_range(bmap, 0, meta_blocks)
        if g == 0:
            # Allocate root dir (1 block), lost+found (1 block), and journal blocks.
            extra_alloc = 2 + journal_blocks
            _set_bitmap_range(bmap, meta_blocks, extra_alloc)
            used_blocks = meta_blocks + extra_alloc
        else:
            used_blocks = meta_blocks

        if g_blocks < BLOCKS_PER_GROUP:
            _set_bitmap_range(bmap, g_blocks, BLOCKS_PER_GROUP - g_blocks)

        free_b = g_blocks - used_blocks
        total_free_blocks += free_b
        total_overhead_blocks += meta_blocks

        imap = bytearray(BLOCK_SIZE)
        imap[INODES_PER_GROUP // 8 :] = b"\xff" * (BLOCK_SIZE - (INODES_PER_GROUP // 8))
        if g == 0:
            _set_bitmap_range(imap, 0, 11)
            free_i = INODES_PER_GROUP - 11
            dirs = 2
            flags = 0
            itu = INODES_PER_GROUP - 11
        else:
            free_i = INODES_PER_GROUP
            dirs = 0
            flags = EXT4_BG_INODE_UNINIT
            itu = INODES_PER_GROUP

        bb_csum = crc32c_raw(fs_seed, bmap)
        ib_csum = crc32c_raw(fs_seed, memoryview(imap)[: INODES_PER_GROUP // 8])

        gd_off = g * DESC_SIZE
        struct.pack_into(
            "<IIIHHHHIHHHH",
            gdt_buf,
            gd_off,
            bb_blk & 0xFFFFFFFF,
            ib_blk & 0xFFFFFFFF,
            it_blk & 0xFFFFFFFF,
            free_b & 0xFFFF,
            free_i & 0xFFFF,
            dirs & 0xFFFF,
            flags,
            0,  # bg_exclude_bitmap_lo
            bb_csum & 0xFFFF,
            ib_csum & 0xFFFF,
            itu & 0xFFFF,
            0,  # bg_checksum placeholder
        )
        struct.pack_into(
            "<IIIHHHHIHHI",
            gdt_buf,
            gd_off + 32,
            (bb_blk >> 32) & 0xFFFFFFFF,
            (ib_blk >> 32) & 0xFFFFFFFF,
            (it_blk >> 32) & 0xFFFFFFFF,
            (free_b >> 16) & 0xFFFF,
            (free_i >> 16) & 0xFFFF,
            (dirs >> 16) & 0xFFFF,
            (itu >> 16) & 0xFFFF,
            0,  # bg_exclude_bitmap_hi
            (bb_csum >> 16) & 0xFFFF,
            (ib_csum >> 16) & 0xFFFF,
            0,  # bg_reserved
        )

        gd_slice = gdt_buf[gd_off : gd_off + DESC_SIZE]
        gd_csum = crc32c_raw(fs_seed, struct.pack("<I", g))
        gd_csum = crc32c_raw(gd_csum, memoryview(gd_slice)[:0x1E])
        gd_csum = crc32c_raw(gd_csum, b"\x00\x00")
        gd_csum = crc32c_raw(gd_csum, memoryview(gd_slice)[0x20:DESC_SIZE])
        struct.pack_into("<H", gdt_buf, gd_off + 0x1E, gd_csum & 0xFFFF)

        # Write per-group bitmaps; only group 0 needs inode table zeroing because g > 0 sets INODE_UNINIT.
        write_at(partition_offset + bb_blk * BLOCK_SIZE, bmap)
        write_at(partition_offset + ib_blk * BLOCK_SIZE, imap)

        if g == 0:
            it_start_off = it_blk * BLOCK_SIZE + BLOCK_SIZE
            it_rem = INODE_TABLE_BLOCKS * BLOCK_SIZE - BLOCK_SIZE
            it_pos = 0
            while it_pos < it_rem:
                step = min(len(zero_1m), it_rem - it_pos)
                write_at(partition_offset + it_start_off + it_pos, memoryview(zero_1m)[:step])
                it_pos += step

    inodes_count = num_groups * INODES_PER_GROUP
    r_blocks_count = (total_blocks * 5) // 100
    journal_bytes = journal_blocks * BLOCK_SIZE

    root_inode = _pack_inode(
        ino=2,
        fs_seed=fs_seed,
        mode=0o040755,
        size_bytes=BLOCK_SIZE,
        links_count=3,
        blocks_512=BLOCK_SIZE // 512,
        timestamp=now,
        extent_start_block=root_dir_blk,
        extent_block_count=1,
    )
    journal_inode = _pack_inode(
        ino=8,
        fs_seed=fs_seed,
        mode=0o100600,
        size_bytes=journal_bytes,
        links_count=1,
        blocks_512=journal_bytes // 512,
        timestamp=now,
        extent_start_block=journal_start_blk,
        extent_block_count=journal_blocks,
    )
    lost_found_inode = _pack_inode(
        ino=11,
        fs_seed=fs_seed,
        mode=0o040700,
        size_bytes=BLOCK_SIZE,
        links_count=2,
        blocks_512=BLOCK_SIZE // 512,
        timestamp=now,
        extent_start_block=lost_found_blk,
        extent_block_count=1,
    )

    g0_itable_blk0 = bytearray(BLOCK_SIZE)
    g0_itable_blk0[1 * INODE_SIZE : 2 * INODE_SIZE] = root_inode
    g0_itable_blk0[7 * INODE_SIZE : 8 * INODE_SIZE] = journal_inode
    g0_itable_blk0[10 * INODE_SIZE : 11 * INODE_SIZE] = lost_found_inode
    g0_it_blk = 1 + gdt_blocks + 2
    write_at(partition_offset + g0_it_blk * BLOCK_SIZE, g0_itable_blk0)

    root_dir_data = _build_dir_block(
        dir_ino=2,
        parent_ino=2,
        fs_seed=fs_seed,
        extra_entries=((11, "lost+found", 2),),
    )
    lost_found_data = _build_dir_block(dir_ino=11, parent_ino=2, fs_seed=fs_seed)
    write_at(partition_offset + root_dir_blk * BLOCK_SIZE, root_dir_data)
    write_at(partition_offset + lost_found_blk * BLOCK_SIZE, lost_found_data)

    jsb = _build_journal_superblock(uid, journal_blocks)
    write_at(partition_offset + journal_start_blk * BLOCK_SIZE, jsb)
    j_zero_rem = (journal_blocks - 1) * BLOCK_SIZE
    j_zero_off = partition_offset + (journal_start_blk + 1) * BLOCK_SIZE
    j_pos = 0
    while j_pos < j_zero_rem:
        step = min(len(zero_1m), j_zero_rem - j_pos)
        write_at(j_zero_off + j_pos, memoryview(zero_1m)[:step])
        j_pos += step

    # Build base 1024-byte superblock template.
    sb = bytearray(1024)
    struct.pack_into(
        "<IIIIIIIIIIIII",
        sb,
        0x00,
        inodes_count & 0xFFFFFFFF,
        total_blocks & 0xFFFFFFFF,
        r_blocks_count & 0xFFFFFFFF,
        total_free_blocks & 0xFFFFFFFF,
        (inodes_count - 11) & 0xFFFFFFFF,
        0,  # s_first_data_block
        2,  # s_log_block_size (4096)
        2,  # s_log_cluster_size
        BLOCKS_PER_GROUP,
        BLOCKS_PER_GROUP,
        INODES_PER_GROUP,
        0,  # s_mtime
        now,  # s_wtime
    )
    struct.pack_into(
        "<HHHHHHIIII",
        sb,
        0x34,
        0,  # s_mnt_count
        0xFFFF,  # s_max_mnt_count
        0xEF53,  # s_magic
        1,  # s_state (EXT4_VALID_FS)
        1,  # s_errors (continue)
        0,  # s_minor_rev_level
        now,  # s_lastcheck
        0,  # s_checkinterval
        0,  # s_creator_os (Linux)
        1,  # s_rev_level (DYNAMIC_REV)
    )
    struct.pack_into(
        "<HHIHHIII",
        sb,
        0x50,
        0,  # s_def_resuid
        0,  # s_def_resgid
        11,  # s_first_ino
        INODE_SIZE,
        0,  # s_block_group_nr (filled per copy)
        EXT4_COMPAT,
        EXT4_INCOMPAT,
        EXT4_RO_COMPAT,
    )
    sb[0x68:0x78] = uid.bytes
    label_raw = label.encode("utf-8")[:16].ljust(16, b"\x00")
    sb[0x78:0x88] = label_raw
    struct.pack_into("<I", sb, 0xE0, 8)  # s_jnl_inum
    sb[0xEC:0xFC] = hash_seed
    sb[0xFC] = 1  # s_def_hash_version (half_md4)
    sb[0xFD] = 1  # s_jnl_backup_type
    struct.pack_into("<H", sb, 0xFE, DESC_SIZE)
    struct.pack_into("<II", sb, 0x100, 0x000C, 0)  # s_default_mount_opts (user_xattr | acl), s_first_meta_bg
    struct.pack_into("<I", sb, 0x108, now)  # s_mkfs_time
    sb[0x10C:0x148] = journal_inode[0x28:0x64]
    struct.pack_into("<II", sb, 0x148, (journal_bytes >> 32) & 0xFFFFFFFF, journal_bytes & 0xFFFFFFFF)
    struct.pack_into(
        "<IIIHHI",
        sb,
        0x150,
        (total_blocks >> 32) & 0xFFFFFFFF,
        (r_blocks_count >> 32) & 0xFFFFFFFF,
        (total_free_blocks >> 32) & 0xFFFFFFFF,
        32,  # s_min_extra_isize
        32,  # s_want_extra_isize
        0x0001,  # s_flags (signed_directory_hash)
    )
    sb[0x175] = 1  # s_checksum_type (CRC32C)
    struct.pack_into("<Q", sb, 0x178, 1)  # s_kbytes_written
    struct.pack_into("<I", sb, 0x24C, total_overhead_blocks)

    for g in sparse_groups:
        sb_copy = bytearray(sb)
        struct.pack_into("<H", sb_copy, 0x5A, g)
        sb_csum = crc32c_raw(0xFFFFFFFF, memoryview(sb_copy)[:0x3FC])
        struct.pack_into("<I", sb_copy, 0x3FC, sb_csum)

        if g == 0:
            blk0 = bytearray(BLOCK_SIZE)
            blk0[1024:2048] = sb_copy
            write_at(partition_offset, blk0)
            write_at(partition_offset + BLOCK_SIZE, gdt_buf)
        else:
            g_off = partition_offset + g * BLOCKS_PER_GROUP * BLOCK_SIZE
            blk0 = bytearray(BLOCK_SIZE)
            blk0[:1024] = sb_copy
            write_at(g_off, blk0)
            write_at(g_off + BLOCK_SIZE, gdt_buf)
