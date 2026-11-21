from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import threading
import zipfile
import pytest

from flexkit.privilege import _HelperBridge
from flexkit.writer import WriteError, stream_zip_to_fd

_O_BIN = getattr(os, "O_BINARY", 0)


def test_stream_zip_to_fd_and_readback_verify(tmp_path: Path) -> None:
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_img = os.urandom(6 * 1024 * 1024 + 512)
    expected_sha1 = hashlib.sha1(raw_img).hexdigest()

    archive = tmp_path / "flex.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, raw_img)

    target = tmp_path / "usb_block.img"
    wfd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
    phases: list[str] = []

    try:
        stream_zip_to_fd(
            archive_path=archive,
            member_name=member,
            image_size=len(raw_img),
            expected_sha1=expected_sha1,
            write_fd=wfd,
            open_read_fd=lambda: os.open(target, os.O_RDONLY | _O_BIN),
            progress_cb=lambda phase, _d, _t, _b: phases.append(phase),
        )
    finally:
        os.close(wfd)

    assert target.read_bytes() == raw_img
    assert "write" in phases and "verify" in phases


def test_readback_verify_catches_disk_bitflip(tmp_path: Path) -> None:
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_img = os.urandom(1024 * 1024)

    archive = tmp_path / "flex.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, raw_img)

    target = tmp_path / "bad_usb.img"
    wfd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)

    def flip_byte_on_target() -> None:
        fd = os.open(target, os.O_RDWR | _O_BIN)
        try:
            os.lseek(fd, 4096, os.SEEK_SET)
            orig = os.read(fd, 1)
            os.lseek(fd, 4096, os.SEEK_SET)
            os.write(fd, bytes([orig[0] ^ 0x01]))
        finally:
            os.close(fd)

    try:
        with pytest.raises(WriteError, match="Verification failed"):
            stream_zip_to_fd(
                archive_path=archive,
                member_name=member,
                image_size=len(raw_img),
                expected_sha1="",
                write_fd=wfd,
                open_read_fd=lambda: os.open(target, os.O_RDONLY | _O_BIN),
                before_verify_hook=flip_byte_on_target,
            )
    finally:
        os.close(wfd)


def test_posix_block_io_lseek_fallback_without_pread_pwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "pread", raising=False)
    monkeypatch.delattr(os, "pwrite", raising=False)

    member = "chromeos_fallback.bin"
    raw_img = os.urandom(512 * 1024)
    expected_sha1 = hashlib.sha1(raw_img).hexdigest()

    archive = tmp_path / "fallback.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, raw_img)

    target = tmp_path / "fallback_disk.img"
    wfd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
    try:
        stream_zip_to_fd(
            archive_path=archive,
            member_name=member,
            image_size=len(raw_img),
            expected_sha1=expected_sha1,
            write_fd=wfd,
        )
    finally:
        os.close(wfd)

    assert target.read_bytes() == raw_img


def test_helper_bridge_rejects_unauthenticated_socket() -> None:
    bridge = _HelperBridge("secret-token-123")
    seen_progress: list[tuple[str, int, int]] = []

    def client_sim() -> None:
        # Rogue unauthenticated client connects first and must be dropped.
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=2.0) as rogue:
            rogue.sendall(b'{"token":"wrong","event":"hello"}\n')

        with socket.create_connection(("127.0.0.1", bridge.port), timeout=2.0) as valid:
            valid.sendall(b'{"token":"secret-token-123","event":"hello"}\n')
            valid.sendall(b'{"event":"progress","phase":"write","done":512,"total":1024,"bps":100}\n')
            valid.sendall(b'{"event":"done"}\n')

    t = threading.Thread(target=client_sim, daemon=True)
    t.start()
    try:
        conn = bridge.accept_authenticated(lambda: True, timeout=5.0)
        assert conn is not None
        bridge.consume_events(conn, lambda ph, d, tot, _b: seen_progress.append((ph, d, tot)))
        assert seen_progress == [("write", 512, 1024)]
    finally:
        bridge.close()
        t.join(timeout=2.0)


def test_stream_raw_bin_to_fd_and_readback_verify(tmp_path: Path) -> None:
    raw_img = os.urandom(2 * 1024 * 1024 + 512)
    expected_sha1 = hashlib.sha1(raw_img).hexdigest()
    raw_file = tmp_path / "local_flex.bin"
    raw_file.write_bytes(raw_img)

    target = tmp_path / "usb_raw.img"
    wfd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BIN, 0o600)
    try:
        stream_zip_to_fd(
            archive_path=raw_file,
            member_name="",
            image_size=len(raw_img),
            expected_sha1=expected_sha1,
            write_fd=wfd,
            open_read_fd=lambda: os.open(target, os.O_RDONLY | _O_BIN),
        )
    finally:
        os.close(wfd)

    assert target.read_bytes() == raw_img


def test_qt_worker_signals_handle_64bit_image_sizes() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from flexkit.ui.qt import QtWidgets
    from flexkit.ui.window import VerifyWorker, WriteSpec, WriteWorker

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = _app

    v_worker = VerifyWorker(Path("/nonexistent"))
    received_v: list[tuple[int, int, int]] = []
    v_worker.progress.connect(lambda d, t, b: received_v.append((d, t, b)))
    large_done = 9_445_572_608
    large_total = 9_525_263_872
    v_worker.progress.emit(large_done, large_total, 420_000_000)
    assert received_v == [(large_done, large_total, 420_000_000)]

    spec = WriteSpec(
        archive_path=Path("/nonexistent"),
        member_name="",
        device="/dev/sdz",
        expected_serial="",
        expected_size=32_000_000_000,
        image_size=large_total,
        expected_sha1="0" * 40,
    )
    w_worker = WriteWorker(spec)
    received_w: list[tuple[str, int, int, int]] = []
    w_worker.progress.connect(lambda ph, d, t, b: received_w.append((ph, d, t, b)))
    w_worker.progress.emit("verify", large_done, large_total, 180_000_000)
    assert received_w == [("verify", large_done, large_total, 180_000_000)]
