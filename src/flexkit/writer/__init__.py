from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
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
from typing import IO
import zipfile

from flexkit.drives import _drive_hosts_path, enumerate_all_drives
from flexkit.writer.gpt import (
    CHROMEOS_STATE_TYPE,
    ChromeOSBootOptions,
    SECTOR_SIZE,
    build_protective_mbr,
    generate_chromeos_togo_partitions,
    is_chromeos_drive,
    patch_grub_cfg,
    patch_grub_in_block_device,
    read_chromeos_boot_options,
    read_gpt_table,
    serialize_gpt,
)

logger = logging.getLogger("flexkit.writer")

WRITE_BLOCK_SIZE = 4 * 1024 * 1024
FSYNC_INTERVAL = 32 * 1024 * 1024
CHROMEOS_OS_PAYLOAD_SIZE = 6004736 * 512

WriteProgressCallback = Callable[[str, int, int, int], None]


class WriteError(RuntimeError):
    pass


class WriteCancelled(Exception):
    pass


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
    cros_debug: bool = False
    disable_auto_updates: bool = False
    verbose_boot: bool = False
    timeout: int = 2
    nomodeset: bool = False
    unencrypted_stateful: bool = False
    force_legacy_boot: bool = False

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
            "cros_debug": self.cros_debug,
            "disable_auto_updates": self.disable_auto_updates,
            "verbose_boot": self.verbose_boot,
            "timeout": self.timeout,
            "nomodeset": self.nomodeset,
            "unencrypted_stateful": self.unencrypted_stateful,
            "force_legacy_boot": self.force_legacy_boot,
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
            cros_debug=bool(d.get("cros_debug", False)),
            disable_auto_updates=bool(d.get("disable_auto_updates", False)),
            verbose_boot=bool(d.get("verbose_boot", False)),
            timeout=int(d.get("timeout") or 2),
            nomodeset=bool(d.get("nomodeset", False)),
            unencrypted_stateful=bool(d.get("unencrypted_stateful", False)),
            force_legacy_boot=bool(d.get("force_legacy_boot", False)),
        )


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
    cros_debug: bool = False,
    disable_auto_updates: bool = False,
    verbose_boot: bool = False,
    timeout: int = 2,
    nomodeset: bool = False,
    unencrypted_stateful: bool = False,
    force_legacy_boot: bool = False,
) -> None:
    write_sha1 = hashlib.sha1()
    written = 0
    since_sync = 0
    win_start = time.monotonic()
    win_bytes = 0
    bps = 0

    is_togo = install_mode in ("to_go_fresh", "to_go_update")
    target_write_size = CHROMEOS_OS_PAYLOAD_SIZE if is_togo else image_size
    logger.info(
        "Starting stream_source_to_fd: write_fd=%d, target_size=%d, image_size=%d, mode=%s, verify=%s, sha1=%s",
        write_fd,
        target_write_size,
        image_size,
        install_mode,
        verify_drive,
        expected_sha1,
    )

    if progress_cb is not None:
        progress_cb("write", 0, target_write_size, 0)

    if install_mode == "to_go_fresh" and expected_size > 0:
        total_sectors = expected_size // SECTOR_SIZE
        logger.info("Generating ChromeOS To-Go fresh GPT for total_sectors=%d", total_sectors)
        boot_code = None
        try:
            with open_image_source(archive_path, member_name, image_size) as peek_src:
                first_sector = peek_src.read(SECTOR_SIZE)
                if len(first_sector) >= 446 and any(b != 0 for b in first_sector[:446]):
                    boot_code = first_sector[:446]
        except Exception as exc:
            logger.warning("Could not peek source archive MBR: %s", exc)

        mbr = build_protective_mbr(total_sectors, boot_code=boot_code)
        parts = generate_chromeos_togo_partitions(total_sectors)
        primary_hdr, entries, backup_hdr = serialize_gpt(total_sectors, parts)

        os.pwrite(write_fd, mbr, 0)
        os.pwrite(write_fd, primary_hdr, SECTOR_SIZE)
        os.pwrite(write_fd, entries, 2 * SECTOR_SIZE)
        os.pwrite(write_fd, entries, (total_sectors - 33) * SECTOR_SIZE)
        os.pwrite(write_fd, backup_hdr, (total_sectors - 1) * SECTOR_SIZE)
        os.lseek(write_fd, 34 * SECTOR_SIZE, os.SEEK_SET)

    last_logged_mb = 0
    with open_image_source(archive_path, member_name, image_size) as src:
        if install_mode == "to_go_fresh":
            # Skip LBA 0..33 in the source archive so we don't overwrite our To-Go GPT
            to_skip = 34 * SECTOR_SIZE
            while to_skip > 0:
                discarded = src.read(min(WRITE_BLOCK_SIZE, to_skip))
                if not discarded:
                    break
                write_sha1.update(discarded)
                to_skip -= len(discarded)
                written += len(discarded)

        while written < target_write_size:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            to_read = min(WRITE_BLOCK_SIZE, target_write_size - written)
            chunk = src.read(to_read)
            if not chunk:
                break
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                n = os.write(write_fd, view[offset:])
                if n <= 0:
                    raise WriteError("Short write to target device.")
                offset += n
            write_sha1.update(chunk)
            written += len(chunk)
            since_sync += len(chunk)
            win_bytes += len(chunk)

            if since_sync >= FSYNC_INTERVAL:
                os.fsync(write_fd)
                since_sync = 0

            now = time.monotonic()
            elapsed = now - win_start
            if elapsed >= 0.4:
                inst = int(win_bytes / elapsed)
                bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                win_start = now
                win_bytes = 0
            if progress_cb is not None:
                progress_cb("write", written, target_write_size, bps)

            if written - last_logged_mb >= 500 * 1024 * 1024 or written == target_write_size:
                logger.info(
                    "Write progress: %d / %d bytes (%.1f%%) @ %d bps",
                    written,
                    target_write_size,
                    (written / target_write_size) * 100 if target_write_size > 0 else 0,
                    bps,
                )
                last_logged_mb = written

    if install_mode == "to_go_fresh":
        # Zero out the first 16 MiB of the expanded STATE partition so ChromeOS initializes it cleanly
        state_wipe_len = 16 * 1024 * 1024
        zero_buf = b"\x00" * (1024 * 1024)
        for _ in range(16):
            os.write(write_fd, zero_buf)

    has_custom_boot = (
        cros_debug
        or disable_auto_updates
        or verbose_boot
        or nomodeset
        or (timeout != 2)
        or unencrypted_stateful
        or force_legacy_boot
    )
    if has_custom_boot or is_togo:
        boot_opts = ChromeOSBootOptions(
            cros_debug=cros_debug,
            disable_auto_updates=disable_auto_updates,
            verbose_boot=verbose_boot,
            timeout=timeout,
            nomodeset=nomodeset,
            unencrypted_stateful=unencrypted_stateful,
            force_legacy_boot=force_legacy_boot,
        )
        patch_grub_in_block_device(write_fd, boot_opts)

    os.fsync(write_fd)

    if written != target_write_size:
        raise WriteError("Incomplete image stream written to target.")
    computed_sha1 = write_sha1.hexdigest().lower()
    if expected_sha1 and computed_sha1 != expected_sha1.lower() and install_mode == "installer" and not has_custom_boot:
        raise WriteError("Decompressed image SHA-1 did not match expected digest.")

    if not verify_drive:
        logger.info("Verification skipped by user option.")
        return

    if before_verify_hook is not None:
        logger.info("Calling before_verify_hook...")
        before_verify_hook()

    read_fd = open_read_fd() if open_read_fd is not None else write_fd
    need_close_read = open_read_fd is not None
    if not need_close_read:
        os.lseek(write_fd, 0, os.SEEK_SET)

    logger.info("Beginning read-back verification of %d bytes on fd=%d", target_write_size, read_fd)
    last_logged_v_mb = 0
    try:
        verify_sha1 = hashlib.sha1()
        checked = 0
        win_start = time.monotonic()
        win_bytes = 0
        bps = 0
        if progress_cb is not None:
            progress_cb("verify", 0, target_write_size, 0)

        while checked < target_write_size:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            to_read = min(WRITE_BLOCK_SIZE, target_write_size - checked)
            buf = os.read(read_fd, to_read)
            if not buf:
                raise WriteError("Unexpected EOF during read-back verification.")
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
                progress_cb("verify", checked, target_write_size, bps)

            if checked - last_logged_v_mb >= 500 * 1024 * 1024 or checked == target_write_size:
                logger.info(
                    "Verify progress: %d / %d bytes (%.1f%%) @ %d bps",
                    checked,
                    target_write_size,
                    (checked / target_write_size) * 100 if target_write_size > 0 else 0,
                    bps,
                )
                last_logged_v_mb = checked

        v_hex = verify_sha1.hexdigest().lower()
        logger.info("Read-back verification completed. Checksum: %s", v_hex)
        if v_hex != computed_sha1 and install_mode == "installer" and not has_custom_boot:
            raise WriteError("Verification failed: data read back from drive did not match image SHA-1.")
        logger.info("Drive verification successful!")
    finally:
        if need_close_read:
            os.close(read_fd)


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
                elif cmd == "read_boot_options":
                    dev = str(msg.get("device") or "")
                    opts = read_chromeos_boot_options(dev)
                    send_msg({"event": "boot_options", "options": opts.to_dict() if opts else None})
                elif cmd == "patch_boot_options":
                    dev = str(msg.get("device") or "")
                    opts_dict = msg.get("options") or {}
                    ok = patch_grub_in_block_device(dev, ChromeOSBootOptions.from_dict(opts_dict))
                    send_msg({"event": "boot_options_patched", "ok": ok})
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
