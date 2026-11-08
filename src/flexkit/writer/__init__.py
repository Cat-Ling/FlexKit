from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import IO
import zipfile

from flexkit.drives import _drive_hosts_path, enumerate_all_drives

WRITE_BLOCK_SIZE = 4 * 1024 * 1024
FSYNC_INTERVAL = 32 * 1024 * 1024

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
    open_read_fd: Callable[[], int],
    progress_cb: WriteProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    before_verify_hook: Callable[[], None] | None = None,
) -> None:
    write_sha1 = hashlib.sha1()
    written = 0
    since_sync = 0
    win_start = time.monotonic()
    win_bytes = 0
    bps = 0

    if progress_cb is not None:
        progress_cb("write", 0, image_size, 0)

    with open_image_source(archive_path, member_name, image_size) as src:
        while True:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            chunk = src.read(WRITE_BLOCK_SIZE)
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
                progress_cb("write", written, image_size, bps)

    os.fsync(write_fd)

    if written != image_size:
        raise WriteError("Incomplete image stream written to target.")
    computed_sha1 = write_sha1.hexdigest().lower()
    if expected_sha1 and computed_sha1 != expected_sha1.lower():
        raise WriteError("Decompressed image SHA-1 did not match expected digest.")

    if before_verify_hook is not None:
        before_verify_hook()

    read_fd = open_read_fd()
    try:
        verify_sha1 = hashlib.sha1()
        checked = 0
        win_start = time.monotonic()
        win_bytes = 0
        bps = 0
        if progress_cb is not None:
            progress_cb("verify", 0, image_size, 0)

        while checked < image_size:
            if cancel_check is not None and cancel_check():
                raise WriteCancelled()
            to_read = min(WRITE_BLOCK_SIZE, image_size - checked)
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
                progress_cb("verify", checked, image_size, bps)

        if verify_sha1.hexdigest().lower() != computed_sha1:
            raise WriteError("Verification failed: data read back from drive did not match image SHA-1.")
    finally:
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
    parser = argparse.ArgumentParser(prog="flexkit --write", add_help=False)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--member", default="")
    parser.add_argument("--device", required=True)
    parser.add_argument("--serial", default="")
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--image-size", type=int, required=True)
    parser.add_argument("--sha1", default="")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

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
    send_lock = threading.Lock()

    try:
        sock = socket.create_connection(("127.0.0.1", args.port), timeout=10.0)
    except OSError:
        return 2

    def send_msg(payload: dict[str, object]) -> None:
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with send_lock:
            try:
                sock.sendall(line)
            except OSError:
                cancel_event.set()

    def watch_commands() -> None:
        buf = b""
        sock.settimeout(None)
        try:
            while not cancel_event.is_set():
                chunk = sock.recv(1024)
                if not chunk:
                    cancel_event.set()
                    return
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(raw_line.decode("utf-8"))
                        if isinstance(msg, dict) and msg.get("cmd") == "cancel":
                            cancel_event.set()
                            return
                    except json.JSONDecodeError:
                        pass
        except OSError:
            cancel_event.set()

    with sock:
        send_msg({"token": args.token, "event": "hello"})
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
