from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import re
import shutil
import threading
import zipfile
import pytest

from flexkit.catalog import Channel, Release
from flexkit.download import (
    Aria2Downloader,
    DownloadState,
    HttpDownloader,
    VerificationError,
    aria2_binary,
    inspect_existing_download,
    prepare_local_image,
    verify_archive,
)


def _make_zip_fixture(member_name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member_name, payload)
    return buf.getvalue()


class _RangeServer:
    def __init__(self, data: bytes, etag: str = '"v1"') -> None:
        self.data = data
        self.etag = etag
        self.cut_limit: int | None = None
        self.requests: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _fmt: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                rng = self.headers.get("Range")
                if_range = self.headers.get("If-Range")
                outer.requests.append(rng)

                start = 0
                status = 200
                if rng and (not if_range or if_range == outer.etag):
                    m = re.match(r"bytes=(\d+)-", rng)
                    if m:
                        start = int(m.group(1))
                        status = 206

                body = outer.data[start:]
                self.send_response(status)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", outer.etag)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(outer.data) - 1}/{len(outer.data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

                to_send = body if outer.cut_limit is None else body[: outer.cut_limit]
                try:
                    self.wfile.write(to_send)
                    self.wfile.flush()
                except BrokenPipeError:
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _build_release(port: int, zip_bytes: bytes, raw_bin: bytes, member: str) -> Release:
    return Release(
        channel=Channel.STABLE,
        version="16836.30.0",
        chrome_version="156.0.8078.77",
        file=member,
        url=f"http://127.0.0.1:{port}/{member}.zip",
        zip_size=len(zip_bytes),
        image_size=len(raw_bin),
        sha1=hashlib.sha1(zip_bytes).hexdigest(),
        md5=hashlib.md5(zip_bytes).hexdigest(),
    )


def test_http_resume_and_uncommitted_tail_truncation(tmp_path: Path) -> None:
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_bin = os.urandom(3 * 1024 * 1024)
    zip_bytes = _make_zip_fixture(member, raw_bin)

    srv = _RangeServer(zip_bytes)
    try:
        rel = _build_release(srv.port, zip_bytes, raw_bin, member)

        # Cut stream repeatedly after ~450 KB chunks with 256 KB commit intervals.
        srv.cut_limit = 450 * 1024
        dl1 = HttpDownloader(rel, tmp_path, max_retries=1, commit_interval=256 * 1024)
        with pytest.raises(Exception):
            dl1.run()

        state, committed = inspect_existing_download(tmp_path, rel, allow_aria2=False)
        assert state == DownloadState.RESUMABLE
        assert 0 < committed < len(zip_bytes)

        # Simulate crash leaving uncommitted corrupted bytes beyond the fsynced control offset.
        part_path = tmp_path / f"{rel.archive_name}.part"
        with open(part_path, "ab") as fh:
            fh.write(b"CORRUPT_TAIL_DATA" * 500)

        srv.cut_limit = None
        dl2 = HttpDownloader(rel, tmp_path, max_retries=2, commit_interval=256 * 1024)
        out_path = dl2.run()

        assert out_path.read_bytes() == zip_bytes
        assert not part_path.exists()
        assert not (tmp_path / f"{rel.archive_name}.part.json").exists()
        verify_archive(out_path, rel)
    finally:
        srv.close()


def test_http_etag_change_restarts_cleanly(tmp_path: Path) -> None:
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_v1 = b"A" * (1024 * 1024) + os.urandom(512 * 1024)
    raw_v2 = b"B" * (1024 * 1024) + os.urandom(512 * 1024)
    zip_v1 = _make_zip_fixture(member, raw_v1)
    zip_v2 = _make_zip_fixture(member, raw_v2)

    srv = _RangeServer(zip_v1, etag='"etag-1"')
    try:
        rel1 = _build_release(srv.port, zip_v1, raw_v1, member)
        srv.cut_limit = len(zip_v1) // 4
        dl1 = HttpDownloader(rel1, tmp_path, max_retries=1, commit_interval=64 * 1024)
        with pytest.raises(Exception):
            dl1.run()

        # Server updates payload and ETag under same URL.
        srv.data = zip_v2
        srv.etag = '"etag-2"'
        srv.cut_limit = None
        rel2 = _build_release(srv.port, zip_v2, raw_v2, member)
        # Force matching total in existing control file to specifically exercise If-Range -> 200 fallback.
        ctl_path = tmp_path / f"{rel2.archive_name}.part.json"
        ctl_txt = ctl_path.read_text(encoding="utf-8").replace(str(len(zip_v1)), str(len(zip_v2)))
        ctl_path.write_text(ctl_txt, encoding="utf-8")

        dl2 = HttpDownloader(rel2, tmp_path, max_retries=2, commit_interval=256 * 1024)
        out = dl2.run()
        assert out.read_bytes() == zip_v2
        verify_archive(out, rel2)
    finally:
        srv.close()


def test_verify_archive_detects_corruption(tmp_path: Path) -> None:
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_bin = os.urandom(512 * 1024)
    zip_bytes = bytearray(_make_zip_fixture(member, raw_bin))
    rel = _build_release(9999, bytes(zip_bytes), raw_bin, member)

    zip_file = tmp_path / rel.archive_name
    zip_bytes[len(zip_bytes) // 2] ^= 0xFF
    zip_file.write_bytes(bytes(zip_bytes))

    with pytest.raises(VerificationError):
        verify_archive(zip_file, rel)


@pytest.mark.skipif(shutil.which("aria2c") is None, reason="aria2c not installed")
def test_aria2_download_and_prealloc_guard(tmp_path: Path) -> None:
    assert aria2_binary() is not None
    member = "chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"
    raw_bin = os.urandom(1024 * 1024)
    zip_bytes = _make_zip_fixture(member, raw_bin)

    srv = _RangeServer(zip_bytes)
    try:
        rel = _build_release(srv.port, zip_bytes, raw_bin, member)

        # A preallocated file accompanied by .aria2 must be treated as RESUMABLE, never COMPLETE.
        prealloc = tmp_path / rel.archive_name
        aria_ctl = tmp_path / f"{rel.archive_name}.aria2"
        prealloc.write_bytes(b"\x00" * len(zip_bytes))
        aria_ctl.write_bytes(b"\x00\x01")
        state, _ = inspect_existing_download(tmp_path, rel, allow_aria2=True)
        assert state == DownloadState.RESUMABLE
        prealloc.unlink()
        aria_ctl.unlink()

        dl = Aria2Downloader(rel, tmp_path, max_tries=2)
        out = dl.run()
        assert out.read_bytes() == zip_bytes
        verify_archive(out, rel)
    finally:
        srv.close()


def test_prepare_local_image_zip_raw_and_foreign_board_rejection(tmp_path: Path) -> None:
    flex_member = "chromeos_16503.60.0_reven_recovery_stable-channel_mp-v2.bin"
    raw_bin = os.urandom(512 * 1024)
    zip_bytes = _make_zip_fixture(flex_member, raw_bin)

    local_zip = tmp_path / f"{flex_member}.zip"
    local_zip.write_bytes(zip_bytes)
    prep_zip = prepare_local_image(local_zip)
    assert prep_zip.is_local is True
    assert prep_zip.member_name == flex_member
    assert prep_zip.image_size == len(raw_bin)
    assert prep_zip.sha1 == ""
    assert prep_zip.title == "ChromeOS Flex 16503.60.0"

    catalog_rel = _build_release(9999, zip_bytes, raw_bin, flex_member)
    prep_cat_zip = prepare_local_image(local_zip, [catalog_rel])
    assert prep_cat_zip.title == "ChromeOS Flex 16836.30.0"

    corrupt_zip = tmp_path / "corrupt.zip"
    tampered = bytearray(zip_bytes)
    tampered[len(tampered) // 2] ^= 0xFF
    corrupt_zip.write_bytes(bytes(tampered))
    with pytest.raises(VerificationError):
        prepare_local_image(corrupt_zip, [catalog_rel])

    local_bin = tmp_path / flex_member
    local_bin.write_bytes(raw_bin)
    prep_raw = prepare_local_image(local_bin)
    assert prep_raw.is_local is True
    assert prep_raw.member_name == ""
    assert prep_raw.image_size == len(raw_bin)
    assert prep_raw.sha1 == ""

    chromebook_bin = tmp_path / "chromeos_16503.60.0_octopus_recovery_stable-channel_mp-v33.bin"
    chromebook_bin.write_bytes(raw_bin)
    with pytest.raises(VerificationError, match="not ChromeOS Flex"):
        prepare_local_image(chromebook_bin)
