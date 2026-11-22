from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import IO, Protocol
import zipfile

from flexkit.drives import _drive_hosts_path, enumerate_all_drives
from flexkit.writer.ext4 import format_ext4_partition, verify_ext4_etc_lsb_release
from flexkit.writer.gpt import (
    SECTOR_SIZE,
    GptPartition,
    GptTable,
    build_protective_mbr,
    plan_chromeos_togo_layout,
    plan_chromeos_togo_update,
    read_gpt_table_from_bytes,
    serialize_gpt,
)

logger = logging.getLogger("flexkit.writer")

WRITE_BLOCK_SIZE = 4 * 1024 * 1024
FSYNC_INTERVAL = 32 * 1024 * 1024
MIN_TOGO_DRIVE_SIZE = 11 * 1024 * 1024 * 1024
DISABLE_UPDATES_LSB_RELEASE = b"CHROMEOS_RELEASE_VERSION=99999.0.0\n"

WriteProgressCallback = Callable[[str, int, int, int], None]


class WriteError(RuntimeError):
    pass


class WriteCancelled(Exception):
    pass


class BlockDeviceIO(Protocol):
    @property
    def size(self) -> int: ...

    def write_at(self, offset: int, data: bytes | memoryview) -> None: ...

    def read_at(self, offset: int, size: int) -> bytes: ...

    def flush(self) -> None: ...


class PosixBlockIO:
    def __init__(
        self,
        write_fd: int,
        size: int,
        open_read_fd: Callable[[], int] | None = None,
    ) -> None:
        self._write_fd = write_fd
        self._size = size
        self._open_read_fd = open_read_fd
        self._read_fd: int | None = None
        self._owns_read_fd = False
        self._io_lock = threading.Lock()

    @property
    def size(self) -> int:
        return self._size

    def _get_read_fd(self) -> int:
        if self._read_fd is not None:
            return self._read_fd
        if self._open_read_fd is not None:
            self._read_fd = self._open_read_fd()
            self._owns_read_fd = True
            return self._read_fd
        return self._write_fd

    def reopen_for_read(self) -> None:
        self.close_read_fd()
        if self._open_read_fd is not None:
            self._read_fd = self._open_read_fd()
            self._owns_read_fd = True

    def close_read_fd(self) -> None:
        if self._owns_read_fd and self._read_fd is not None:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            self._read_fd = None
            self._owns_read_fd = False

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        view = memoryview(data)
        pos = 0
        total = len(view)
        pwrite = getattr(os, "pwrite", None)
        while pos < total:
            if pwrite is not None:
                n = pwrite(self._write_fd, view[pos:], offset + pos)
            else:
                with self._io_lock:
                    os.lseek(self._write_fd, offset + pos, os.SEEK_SET)
                    n = os.write(self._write_fd, view[pos:])
            if n <= 0:
                raise WriteError("Short write to target device.")
            pos += n

    def read_at(self, offset: int, size: int) -> bytes:
        fd = self._get_read_fd()
        out = bytearray(size)
        view = memoryview(out)
        pos = 0
        pread = getattr(os, "pread", None)
        while pos < size:
            if pread is not None:
                chunk = pread(fd, size - pos, offset + pos)
            else:
                with self._io_lock:
                    os.lseek(fd, offset + pos, os.SEEK_SET)
                    chunk = os.read(fd, size - pos)
            if not chunk:
                raise WriteError("Unexpected EOF during read-back verification.")
            n = len(chunk)
            view[pos : pos + n] = chunk
            pos += n
        return bytes(out)

    def flush(self) -> None:
        os.fsync(self._write_fd)


@dataclass(frozen=True, slots=True)
class WriteSpec:
    archive_path: Path
    member_name: str
    device: str
    expected_serial: str
    expected_size: int
    image_size: int
    expected_sha1: str = ""
    verify_drive: bool = True
    install_mode: str = "installer"
    install_flexshim: bool = False
    cros_debug: bool = False
    disable_updates: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_path": str(self.archive_path.resolve()),
            "member_name": self.member_name,
            "device": self.device,
            "expected_serial": self.expected_serial,
            "expected_size": self.expected_size,
            "image_size": self.image_size,
            "expected_sha1": self.expected_sha1,
            "verify_drive": self.verify_drive,
            "install_mode": self.install_mode,
            "install_flexshim": self.install_flexshim,
            "cros_debug": self.cros_debug,
            "disable_updates": self.disable_updates,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> WriteSpec:
        return cls(
            archive_path=Path(str(d["archive_path"])),
            member_name=str(d.get("member_name") or ""),
            device=str(d["device"]),
            expected_serial=str(d.get("expected_serial") or ""),
            expected_size=int(d["expected_size"]),
            image_size=int(d["image_size"]),
            expected_sha1=str(d.get("expected_sha1") or "").lower(),
            verify_drive=bool(d.get("verify_drive", True)),
            install_mode=str(d.get("install_mode") or "installer"),
            install_flexshim=bool(d.get("install_flexshim", False)),
            cros_debug=bool(d.get("cros_debug", False)),
            disable_updates=bool(d.get("disable_updates", False)),
        )


@dataclass(frozen=True, slots=True)
class _CopyOp:
    name: str
    src_offset: int
    length: int
    dst_offsets: tuple[int, ...]


def verify_target_device(spec: WriteSpec) -> None:
    drives = enumerate_all_drives()
    matched = [d for d in drives if d.id == spec.device]
    if len(matched) != 1:
        raise WriteError(f"Target drive {spec.device} is no longer connected.")
    drv = matched[0]
    if drv.system:
        raise WriteError("Refusing to write to a system disk.")
    if drv.read_only:
        raise WriteError("Target drive is write-protected.")
    if drv.bus not in ("USB", "SD"):
        raise WriteError(f"Refusing to write to non-USB bus ({drv.bus}).")
    if drv.size != spec.expected_size or drv.serial != spec.expected_serial:
        raise WriteError("Drive identity changed since confirmation; aborting for safety.")

    if spec.install_mode == "to_go_fresh":
        min_req = max(spec.image_size, MIN_TOGO_DRIVE_SIZE)
        if drv.size < min_req:
            raise WriteError("Target drive is too small for ChromeOS To Go (16 GB or larger required).")
    elif spec.install_mode == "to_go_update":
        if drv.size < spec.image_size:
            raise WriteError("Target drive is smaller than the ChromeOS Flex image.")
    else:
        if drv.size < spec.image_size:
            raise WriteError("Target drive is smaller than the ChromeOS Flex image.")

    if _drive_hosts_path(drv, spec.archive_path):
        raise WriteError("Refusing to overwrite the drive holding the downloaded archive.")


@contextmanager
def open_image_source(archive_path: Path, member_name: str, image_size: int) -> Iterator[IO[bytes]]:
    if not archive_path.is_file():
        raise WriteError("Archive file not found.")
    if member_name:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                info = zf.getinfo(member_name)
            except KeyError as exc:
                raise WriteError(f"Image {member_name!r} missing from archive.") from exc
            if info.file_size != image_size:
                raise WriteError("Archive member size does not match expected image size.")
            with zf.open(info, "r") as src:
                yield src
    else:
        if archive_path.stat().st_size != image_size:
            raise WriteError("Image file size does not match expected image size.")
        with open(archive_path, "rb") as src:
            yield src


def _read_exact(src: IO[bytes], count: int) -> bytes:
    out = bytearray()
    while len(out) < count:
        chunk = src.read(count - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _skip_bytes(src: IO[bytes], count: int, cancel_check: Callable[[], bool] | None) -> None:
    rem = count
    while rem > 0:
        if cancel_check is not None and cancel_check():
            raise WriteCancelled()
        chunk = src.read(min(WRITE_BLOCK_SIZE, rem))
        if not chunk:
            raise WriteError("Unexpected end of source image while advancing stream.")
        rem -= len(chunk)


def _build_togo_copy_ops(source_gpt: GptTable, dst_parts: Sequence[GptPartition]) -> list[_CopyOp]:
    dst_by_num = {p.number: p for p in dst_parts}
    ops: list[_CopyOp] = []

    for p_num in (11, 6, 7, 9, 10):
        src_p = source_gpt.get_by_num(p_num)
        dst_p = dst_by_num.get(p_num)
        if src_p is not None and dst_p is not None and src_p.size_bytes > 0 and dst_p.size_bytes >= src_p.size_bytes:
            ops.append(
                _CopyOp(
                    name=src_p.name or f"P{p_num}",
                    src_offset=src_p.start_lba * SECTOR_SIZE,
                    length=src_p.size_bytes,
                    dst_offsets=(dst_p.start_lba * SECTOR_SIZE,),
                )
            )

    src_kern = source_gpt.get_by_name("KERN-B") or source_gpt.get_by_name("KERN-A")
    dst_kern_a = dst_by_num.get(2)
    dst_kern_b = dst_by_num.get(4)
    if src_kern is None or dst_kern_a is None or dst_kern_b is None:
        raise WriteError("Missing KERN partitions for ChromeOS To Go.")
    ops.append(
        _CopyOp(
            name="KERN-A/B",
            src_offset=src_kern.start_lba * SECTOR_SIZE,
            length=src_kern.size_bytes,
            dst_offsets=(dst_kern_a.start_lba * SECTOR_SIZE, dst_kern_b.start_lba * SECTOR_SIZE),
        )
    )

    src_oem = source_gpt.get_by_name("OEM")
    dst_oem = dst_by_num.get(8)
    if src_oem is None or dst_oem is None:
        raise WriteError("Missing OEM partition for ChromeOS To Go.")
    ops.append(
        _CopyOp(
            name="OEM",
            src_offset=src_oem.start_lba * SECTOR_SIZE,
            length=src_oem.size_bytes,
            dst_offsets=(dst_oem.start_lba * SECTOR_SIZE,),
        )
    )

    src_efi = source_gpt.get_by_name("EFI-SYSTEM")
    dst_efi = dst_by_num.get(12)
    if src_efi is None or dst_efi is None:
        raise WriteError("Missing EFI-SYSTEM partition for ChromeOS To Go.")
    ops.append(
        _CopyOp(
            name="EFI-SYSTEM",
            src_offset=src_efi.start_lba * SECTOR_SIZE,
            length=src_efi.size_bytes,
            dst_offsets=(dst_efi.start_lba * SECTOR_SIZE,),
        )
    )

    src_root_a = source_gpt.get_by_name("ROOT-A")
    dst_root_b = dst_by_num.get(5)
    dst_root_a = dst_by_num.get(3)
    if src_root_a is None or dst_root_b is None or dst_root_a is None:
        raise WriteError("Missing ROOT partitions for ChromeOS To Go.")
    ops.append(
        _CopyOp(
            name="ROOT-A/B",
            src_offset=src_root_a.start_lba * SECTOR_SIZE,
            length=src_root_a.size_bytes,
            dst_offsets=(dst_root_b.start_lba * SECTOR_SIZE, dst_root_a.start_lba * SECTOR_SIZE),
        )
    )

    ops.sort(key=lambda op: op.src_offset)
    for idx in range(len(ops) - 1):
        cur = ops[idx]
        nxt = ops[idx + 1]
        if cur.src_offset + cur.length > nxt.src_offset:
            raise WriteError(f"Overlapping source partitions ({cur.name} and {nxt.name}).")
    return ops


def _stream_installer_to_io(
    *,
    archive_path: Path,
    member_name: str,
    image_size: int,
    expected_sha1: str,
    io: BlockDeviceIO,
    progress_cb: WriteProgressCallback | None,
    cancel_check: Callable[[], bool] | None,
    before_verify_hook: Callable[[], None] | None,
    verify_drive: bool,
    install_flexshim: bool = False,
    cros_debug: bool = False,
    disable_updates: bool = False,
) -> None:
    if disable_updates:
        raise WriteError("Disabling ChromeOS updates is only supported for ChromeOS To Go fresh installs.")
    write_sha1 = hashlib.sha1()
    written = 0
    since_sync = 0
    win_start = time.monotonic()
    win_bytes = 0
    bps = 0
    last_logged_mb = 0

    if progress_cb is not None:
        progress_cb("write", 0, image_size, 0)

    with open_image_source(archive_path, member_name, image_size) as src:
        while written < image_size:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            chunk = src.read(min(WRITE_BLOCK_SIZE, image_size - written))
            if not chunk:
                break
            io.write_at(written, chunk)
            write_sha1.update(chunk)
            written += len(chunk)
            since_sync += len(chunk)
            win_bytes += len(chunk)

            if since_sync >= FSYNC_INTERVAL:
                io.flush()
                since_sync = 0

            now = time.monotonic()
            elapsed = now - win_start
            if elapsed >= 0.4:
                inst = int(win_bytes / elapsed)
                bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                win_start = now
                win_bytes = 0
            if progress_cb is not None:
                progress_cb("write", written, image_size, bps)

            if written - last_logged_mb >= 500 * 1024 * 1024 or written == image_size:
                logger.info(
                    "Write progress: %d / %d bytes (%.1f%%) @ %d bps",
                    written,
                    image_size,
                    (written / image_size) * 100 if image_size > 0 else 0,
                    bps,
                )
                last_logged_mb = written

    io.flush()

    if written != image_size:
        raise WriteError("Incomplete image stream written to target.")
    computed_sha1 = write_sha1.hexdigest().lower()
    if expected_sha1 and computed_sha1 != expected_sha1.lower():
        raise WriteError("Decompressed image SHA-1 did not match expected digest.")

    if verify_drive:
        if before_verify_hook is not None:
            logger.info("Calling before_verify_hook...")
            before_verify_hook()

        if isinstance(io, PosixBlockIO):
            io.reopen_for_read()

        logger.info("Beginning read-back verification of %d bytes", image_size)
        verify_sha1 = hashlib.sha1()
        checked = 0
        win_start = time.monotonic()
        win_bytes = 0
        bps = 0
        last_logged_v_mb = 0
        if progress_cb is not None:
            progress_cb("verify", 0, image_size, 0)

        while checked < image_size:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            to_read = min(WRITE_BLOCK_SIZE, image_size - checked)
            buf = io.read_at(checked, to_read)
            verify_sha1.update(buf)
            checked += len(buf)
            win_bytes += len(buf)

            now = time.monotonic()
            elapsed = now - win_start
            if elapsed >= 0.4:
                inst = int(win_bytes / elapsed)
                bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                win_start = now
                win_bytes = 0
            if progress_cb is not None:
                progress_cb("verify", checked, image_size, bps)

            if checked - last_logged_v_mb >= 500 * 1024 * 1024 or checked == image_size:
                logger.info(
                    "Verify progress: %d / %d bytes (%.1f%%) @ %d bps",
                    checked,
                    image_size,
                    (checked / image_size) * 100 if image_size > 0 else 0,
                    bps,
                )
                last_logged_v_mb = checked

        v_hex = verify_sha1.hexdigest().lower()
        logger.info("Read-back verification completed. Checksum: %s", v_hex)
        if v_hex != computed_sha1:
            raise WriteError("Verification failed: data read back from drive did not match image SHA-1.")
        logger.info("Drive verification successful!")
    else:
        logger.info("Verification skipped by user option.")

    if install_flexshim or cros_debug:
        from flexkit.writer.flexshim import install_flexshim_to_esp, verify_flexshim_on_esp

        hdr_buf = io.read_at(0, 34 * SECTOR_SIZE)
        table = read_gpt_table_from_bytes(hdr_buf)
        if table is None or not table.is_chromeos:
            raise WriteError("Image does not contain a valid ChromeOS GPT table to install flexshim.")
        efi_part = table.get_by_name("EFI-SYSTEM")
        if efi_part is None:
            raise WriteError("Missing EFI-SYSTEM partition for flexshim installation.")
        esp_offset = efi_part.start_lba * SECTOR_SIZE
        esp_size = efi_part.size_bytes
        logger.info("Installing flexshim into EFI-SYSTEM at offset %d (cros_debug=%s)...", esp_offset, cros_debug)
        efi_bytes, cfg_bytes = install_flexshim_to_esp(io, esp_offset, esp_size, cros_debug=cros_debug)
        io.flush()
        if verify_drive:
            if before_verify_hook is not None:
                before_verify_hook()
            if isinstance(io, PosixBlockIO):
                io.reopen_for_read()
            verify_flexshim_on_esp(io, esp_offset, esp_size, efi_bytes, cfg_bytes)
            logger.info("flexshim verification successful!")


def _stream_togo_to_io(
    *,
    archive_path: Path,
    member_name: str,
    image_size: int,
    io: BlockDeviceIO,
    install_mode: str,
    expected_size: int,
    progress_cb: WriteProgressCallback | None,
    cancel_check: Callable[[], bool] | None,
    before_verify_hook: Callable[[], None] | None,
    verify_drive: bool,
    install_flexshim: bool = False,
    cros_debug: bool = False,
    disable_updates: bool = False,
) -> None:
    if disable_updates and install_mode != "to_go_fresh":
        raise WriteError("Disabling ChromeOS updates is only supported for ChromeOS To Go fresh installs.")
    effective_cros_debug = cros_debug or disable_updates
    effective_flexshim = install_flexshim or effective_cros_debug

    disk_bytes = expected_size if expected_size > 0 else io.size
    total_sectors = disk_bytes // SECTOR_SIZE
    if total_sectors < 1000:
        raise WriteError("Invalid target drive size for ChromeOS To Go.")

    target_gpt_existing: GptTable | None = None
    if install_mode == "to_go_update":
        hdr_buf = io.read_at(0, 34 * SECTOR_SIZE)
        target_gpt_existing = read_gpt_table_from_bytes(hdr_buf)
        if target_gpt_existing is None or not target_gpt_existing.is_chromeos_togo:
            raise WriteError("Target drive does not have a valid ChromeOS To Go installation to update.")

    with open_image_source(archive_path, member_name, image_size) as src:
        src_hdr = _read_exact(src, 34 * SECTOR_SIZE)
        if len(src_hdr) < 34 * SECTOR_SIZE:
            raise WriteError("Source image header is truncated.")
        src_pos = len(src_hdr)

        source_gpt = read_gpt_table_from_bytes(src_hdr)
        if source_gpt is None or not source_gpt.is_chromeos:
            raise WriteError("Source image does not contain a valid ChromeOS GPT table.")

        boot_code = src_hdr[:446] if any(b != 0 for b in src_hdr[:446]) else None

        try:
            if install_mode == "to_go_fresh":
                dst_parts = plan_chromeos_togo_layout(source_gpt, total_sectors)
                disk_guid = None
            else:
                assert target_gpt_existing is not None
                dst_parts = plan_chromeos_togo_update(source_gpt, target_gpt_existing)
                disk_guid = target_gpt_existing.disk_guid
        except ValueError as exc:
            raise WriteError(str(exc)) from exc

        dst_efi = next((p for p in dst_parts if p.number == 12), None)
        dst_state = next((p for p in dst_parts if p.number == 1), None)
        if dst_efi is None or dst_state is None:
            raise WriteError("Planned ChromeOS To Go layout is missing EFI-SYSTEM or STATE.")

        copy_ops = _build_togo_copy_ops(source_gpt, dst_parts)
        total_write_bytes = sum(op.length * len(op.dst_offsets) for op in copy_ops)
        if total_write_bytes <= 0:
            raise WriteError("No partitions scheduled to write.")

        mbr = build_protective_mbr(
            total_sectors,
            boot_code=boot_code,
            efi_lba=dst_efi.start_lba,
            efi_sectors=dst_efi.sector_count,
        )
        primary_hdr, entries, backup_hdr = serialize_gpt(total_sectors, dst_parts, disk_guid=disk_guid)

        if install_mode == "to_go_fresh":
            wipe_64 = b"\x00" * (64 * SECTOR_SIZE)
            io.write_at(0, wipe_64)
            io.write_at((total_sectors - 64) * SECTOR_SIZE, wipe_64)
            io.write_at(0, mbr)
            io.write_at(SECTOR_SIZE, primary_hdr)
            io.write_at(2 * SECTOR_SIZE, entries)
            io.write_at((total_sectors - 33) * SECTOR_SIZE, entries)
            io.write_at((total_sectors - 1) * SECTOR_SIZE, backup_hdr)

        if progress_cb is not None:
            progress_cb("write", 0, total_write_bytes, 0)

        written = 0
        since_sync = 0
        win_start = time.monotonic()
        win_bytes = 0
        bps = 0
        last_logged_mb = 0
        op_sha1s: list[str] = []

        for op in copy_ops:
            if op.src_offset > src_pos:
                _skip_bytes(src, op.src_offset - src_pos, cancel_check)
                src_pos = op.src_offset

            op_hash = hashlib.sha1()
            op_pos = 0
            while op_pos < op.length:
                if cancel_check is not None and cancel_check():
                    raise WriteCancelled()
                want = min(WRITE_BLOCK_SIZE, op.length - op_pos)
                chunk = _read_exact(src, want)
                if len(chunk) != want:
                    raise WriteError(f"Unexpected end of source stream reading {op.name}.")
                src_pos += want
                op_hash.update(chunk)

                for dst_base in op.dst_offsets:
                    io.write_at(dst_base + op_pos, chunk)
                    written += want
                    since_sync += want
                    win_bytes += want

                op_pos += want
                if since_sync >= FSYNC_INTERVAL:
                    io.flush()
                    since_sync = 0

                now = time.monotonic()
                elapsed = now - win_start
                if elapsed >= 0.4:
                    inst = int(win_bytes / elapsed)
                    bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                    win_start = now
                    win_bytes = 0
                if progress_cb is not None:
                    # Keep progress just shy of 100% until STATE formatting/GPT finalize completes.
                    reported = min(written, max(0, total_write_bytes - 1))
                    progress_cb("write", reported, total_write_bytes, bps)

                if written - last_logged_mb >= 500 * 1024 * 1024:
                    logger.info(
                        "Write progress: %d / %d bytes (%.1f%%) @ %d bps",
                        written,
                        total_write_bytes,
                        (written / total_write_bytes) * 100,
                        bps,
                    )
                    last_logged_mb = written

            op_sha1s.append(op_hash.hexdigest().lower())

    if install_mode == "to_go_fresh":
        logger.info(
            "Formatting STATE (H-STATE ext4) at LBA %d (%d bytes, disable_updates=%s)...",
            dst_state.start_lba,
            dst_state.size_bytes,
            disable_updates,
        )
        format_ext4_partition(
            io.write_at,
            dst_state.start_lba * SECTOR_SIZE,
            dst_state.size_bytes,
            label="H-STATE",
            etc_lsb_release=DISABLE_UPDATES_LSB_RELEASE if disable_updates else None,
        )
    else:
        # Commit updated GPT & MBR after OS partitions are written.
        io.write_at(0, mbr)
        io.write_at(SECTOR_SIZE, primary_hdr)
        io.write_at(2 * SECTOR_SIZE, entries)
        io.write_at((total_sectors - 33) * SECTOR_SIZE, entries)
        io.write_at((total_sectors - 1) * SECTOR_SIZE, backup_hdr)

    io.flush()
    if progress_cb is not None:
        progress_cb("write", total_write_bytes, total_write_bytes, bps)

    if effective_flexshim:
        from flexkit.writer.flexshim import install_flexshim_to_esp, verify_flexshim_on_esp

        esp_offset = dst_efi.start_lba * SECTOR_SIZE
        esp_size = dst_efi.size_bytes
        logger.info("Installing flexshim into To Go EFI-SYSTEM at offset %d (cros_debug=%s)...", esp_offset, effective_cros_debug)
        efi_bytes, cfg_bytes = install_flexshim_to_esp(io, esp_offset, esp_size, cros_debug=effective_cros_debug)
        io.flush()
    else:
        efi_bytes = b""
        cfg_bytes = b""

    if not verify_drive:
        logger.info("Verification skipped by user option.")
        return

    if before_verify_hook is not None:
        logger.info("Calling before_verify_hook...")
        before_verify_hook()

    if isinstance(io, PosixBlockIO):
        io.reopen_for_read()

    if io.read_at(0, SECTOR_SIZE) != mbr:
        raise WriteError("Verification failed: MBR read-back mismatch.")
    if io.read_at(SECTOR_SIZE, SECTOR_SIZE) != primary_hdr:
        raise WriteError("Verification failed: primary GPT header mismatch.")
    if io.read_at(2 * SECTOR_SIZE, len(entries)) != entries:
        raise WriteError("Verification failed: primary GPT entries mismatch.")
    if io.read_at((total_sectors - 33) * SECTOR_SIZE, len(entries)) != entries:
        raise WriteError("Verification failed: backup GPT entries mismatch.")
    if io.read_at((total_sectors - 1) * SECTOR_SIZE, SECTOR_SIZE) != backup_hdr:
        raise WriteError("Verification failed: backup GPT header mismatch.")

    if install_mode == "to_go_fresh":
        state_off = dst_state.start_lba * SECTOR_SIZE
        state_sb = io.read_at(state_off + 1024, 1024)
        if state_sb[0x38:0x3A] != b"\x53\xef":
            raise WriteError("Verification failed: H-STATE ext4 superblock magic missing.")
        if disable_updates:
            try:
                verify_ext4_etc_lsb_release(io.read_at, state_off, DISABLE_UPDATES_LSB_RELEASE)
            except ValueError as exc:
                raise WriteError(f"Verification failed: {exc}") from exc

    total_verify_bytes = total_write_bytes
    checked = 0
    win_start = time.monotonic()
    win_bytes = 0
    bps = 0
    last_logged_v_mb = 0
    if progress_cb is not None:
        progress_cb("verify", 0, total_verify_bytes, 0)

    for op, expected_op_sha1 in zip(copy_ops, op_sha1s, strict=True):
        verify_len = op.length
        # flexshim updates /efi/boot files inside EFI-SYSTEM; verify unmodified region separately below if needed.
        if effective_flexshim and op.name == "EFI-SYSTEM":
            checked += op.length * len(op.dst_offsets)
            continue
        for dst_base in op.dst_offsets:
            v_hash = hashlib.sha1()
            pos = 0
            while pos < verify_len:
                if cancel_check is not None and cancel_check():
                    raise WriteCancelled()
                want = min(WRITE_BLOCK_SIZE, verify_len - pos)
                buf = io.read_at(dst_base + pos, want)
                v_hash.update(buf)
                pos += want
                checked += want
                win_bytes += want

                now = time.monotonic()
                elapsed = now - win_start
                if elapsed >= 0.4:
                    inst = int(win_bytes / elapsed)
                    bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                    win_start = now
                    win_bytes = 0
                if progress_cb is not None:
                    progress_cb("verify", checked, total_verify_bytes, bps)

                if checked - last_logged_v_mb >= 500 * 1024 * 1024 or checked == total_verify_bytes:
                    logger.info(
                        "Verify progress: %d / %d bytes (%.1f%%) @ %d bps",
                        checked,
                        total_verify_bytes,
                        (checked / total_verify_bytes) * 100,
                        bps,
                    )
                    last_logged_v_mb = checked

            if v_hash.hexdigest().lower() != expected_op_sha1:
                raise WriteError(f"Verification failed: data read back from {op.name} did not match source SHA-1.")

    if effective_flexshim:
        verify_flexshim_on_esp(io, dst_efi.start_lba * SECTOR_SIZE, dst_efi.size_bytes, efi_bytes, cfg_bytes)

    logger.info("ChromeOS To Go drive verification successful!")


def write_image_to_block_io(
    *,
    archive_path: Path,
    member_name: str,
    image_size: int,
    expected_sha1: str,
    io: BlockDeviceIO,
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    before_verify_hook: Callable[[], None] | None = None,
    verify_drive: bool = True,
    install_mode: str = "installer",
    expected_size: int = 0,
    install_flexshim: bool = False,
    cros_debug: bool = False,
    disable_updates: bool = False,
) -> None:
    logger.info(
        "Starting write_image_to_block_io: image_size=%d, expected_size=%d, mode=%s, verify=%s, flexshim=%s, cros_debug=%s, disable_updates=%s, sha1=%s",
        image_size,
        expected_size,
        install_mode,
        verify_drive,
        install_flexshim,
        cros_debug,
        disable_updates,
        expected_sha1,
    )
    if install_mode in ("to_go_fresh", "to_go_update"):
        _stream_togo_to_io(
            archive_path=archive_path,
            member_name=member_name,
            image_size=image_size,
            io=io,
            install_mode=install_mode,
            expected_size=expected_size,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            before_verify_hook=before_verify_hook,
            verify_drive=verify_drive,
            install_flexshim=install_flexshim,
            cros_debug=cros_debug,
            disable_updates=disable_updates,
        )
        return

    _stream_installer_to_io(
        archive_path=archive_path,
        member_name=member_name,
        image_size=image_size,
        expected_sha1=expected_sha1,
        io=io,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
        before_verify_hook=before_verify_hook,
        verify_drive=verify_drive,
        install_flexshim=install_flexshim,
        cros_debug=cros_debug,
        disable_updates=disable_updates,
    )


def stream_zip_to_fd(
    *,
    archive_path: Path,
    member_name: str,
    image_size: int,
    expected_sha1: str,
    write_fd: int,
    open_read_fd: Callable[[], int] | None = None,
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    before_verify_hook: Callable[[], None] | None = None,
    verify_drive: bool = True,
    install_mode: str = "installer",
    expected_size: int = 0,
    install_flexshim: bool = False,
    cros_debug: bool = False,
    disable_updates: bool = False,
) -> None:
    disk_bytes = expected_size if expected_size > 0 else os.lseek(write_fd, 0, os.SEEK_END)
    io = PosixBlockIO(write_fd, disk_bytes, open_read_fd=open_read_fd)
    try:
        write_image_to_block_io(
            archive_path=archive_path,
            member_name=member_name,
            image_size=image_size,
            expected_sha1=expected_sha1,
            io=io,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            before_verify_hook=before_verify_hook,
            verify_drive=verify_drive,
            install_mode=install_mode,
            expected_size=disk_bytes,
            install_flexshim=install_flexshim,
            cros_debug=cros_debug,
            disable_updates=disable_updates,
        )
    finally:
        io.close_read_fd()


def execute_write(
    spec: WriteSpec,
    *,
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    verify_target_device(spec)
    if sys.platform == "win32":
        from flexkit.writer.windows import write_physical_drive

        write_physical_drive(spec, progress_cb=progress_cb, cancel_check=cancel_check)
    else:
        from flexkit.writer.posix import write_block_device

        write_block_device(spec, progress_cb=progress_cb, cancel_check=cancel_check)


def run_writer_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="flexkit helper", add_help=False)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--helper-daemon", action="store_true")
    parser.add_argument("--archive", default="")
    parser.add_argument("--member", default="")
    parser.add_argument("--device", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument("--size", type=int, default=0)
    parser.add_argument("--image-size", type=int, default=0)
    parser.add_argument("--sha1", default="")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--log", default=None)
    parser.add_argument("--log-file", default=None)
    args, _ = parser.parse_known_args(argv)

    try:
        sock = socket.create_connection(("127.0.0.1", args.port), timeout=10.0)
        sock.settimeout(None)
    except OSError as exc:
        logger.error("Helper connection to port %d failed: %s", args.port, exc)
        return 2

    logger.info("Privileged helper connected to supervisor on port %d", args.port)

    send_lock = threading.Lock()

    def send_msg(payload: dict[str, object]) -> None:
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with send_lock:
            try:
                sock.sendall(line)
            except OSError:
                pass

    with sock:
        send_msg({"token": args.token, "event": "hello"})

        if args.write and args.archive and args.device:
            spec = WriteSpec(
                archive_path=Path(args.archive),
                member_name=args.member,
                device=args.device,
                expected_serial=args.serial,
                expected_size=args.size,
                image_size=args.image_size,
                expected_sha1=args.sha1.lower(),
            )
            cancel_event = threading.Event()

            def watch_commands() -> None:
                cbuf = b""
                sock.settimeout(None)
                try:
                    while not cancel_event.is_set():
                        cchunk = sock.recv(1024)
                        if not cchunk:
                            cancel_event.set()
                            return
                        cbuf += cchunk
                        while b"\n" in cbuf:
                            raw_line, cbuf = cbuf.split(b"\n", 1)
                            try:
                                msg = json.loads(raw_line.decode("utf-8"))
                                if isinstance(msg, dict) and msg.get("cmd") == "cancel":
                                    cancel_event.set()
                                    return
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    cancel_event.set()

            watcher = threading.Thread(target=watch_commands, daemon=True)
            watcher.start()

            last_sent = 0.0
            last_phase = ""

            def on_progress(phase: str, done: int, total: int, bps: int) -> None:
                nonlocal last_sent, last_phase
                now = time.monotonic()
                if phase != last_phase or done == total or (now - last_sent) >= 0.12:
                    last_phase = phase
                    last_sent = now
                    send_msg({"event": "progress", "phase": phase, "done": done, "total": total, "bps": bps})

            try:
                execute_write(spec, progress_cb=on_progress, cancel_check=cancel_event.is_set)
                send_msg({"event": "done"})
                return 0
            except WriteCancelled:
                send_msg({"event": "cancelled"})
                return 130
            except Exception as exc:
                send_msg({"event": "error", "message": str(exc)})
                return 1

        buf = b""
        active_cancel = threading.Event()

        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                if not raw_line.strip():
                    continue
                try:
                    msg = json.loads(raw_line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                cmd = msg.get("cmd")
                if cmd == "ping":
                    send_msg({"event": "pong"})
                elif cmd == "cancel":
                    active_cancel.set()
                elif cmd == "write":
                    active_cancel.clear()
                    spec_dict = msg.get("spec") or {}
                    try:
                        spec = WriteSpec.from_dict(spec_dict)
                    except Exception as err:
                        send_msg({"event": "error", "message": f"Invalid spec: {err}"})
                        continue

                    last_sent = 0.0
                    last_phase = ""

                    def on_daemon_progress(phase: str, done: int, total: int, bps: int) -> None:
                        nonlocal last_sent, last_phase
                        now = time.monotonic()
                        if phase != last_phase or done == total or (now - last_sent) >= 0.12:
                            last_phase = phase
                            last_sent = now
                            send_msg({"event": "progress", "phase": phase, "done": done, "total": total, "bps": bps})

                    def run_daemon_write() -> None:
                        try:
                            logger.info(
                                "Daemon write starting: device=%s, mode=%s, size=%d, verify=%s",
                                spec.device,
                                spec.install_mode,
                                spec.expected_size,
                                spec.verify_drive,
                            )
                            execute_write(spec, progress_cb=on_daemon_progress, cancel_check=active_cancel.is_set)
                            logger.info("Daemon write completed successfully for %s", spec.device)
                            send_msg({"event": "done"})
                        except WriteCancelled:
                            logger.warning("Daemon write cancelled by user")
                            send_msg({"event": "cancelled"})
                        except Exception as exc:
                            logger.exception("Daemon write failed: %s", exc)
                            send_msg({"event": "error", "message": str(exc)})

                    write_thread = threading.Thread(target=run_daemon_write, daemon=True)
                    write_thread.start()
                elif cmd == "quit":
                    send_msg({"event": "bye"})
                    return 0

        return 0
