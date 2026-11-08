from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

from flexkit import USER_AGENT
from flexkit.catalog import Release, format_bytes

log = logging.getLogger("flexkit.download")

COMMIT_INTERVAL = 8 * 1024 * 1024
READ_CHUNK = 256 * 1024
VERIFY_CHUNK = 4 * 1024 * 1024
SPACE_MARGIN = 32 * 1024 * 1024
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_RECOVERY_BOARD_RE = re.compile(r"^chromeos_([0-9.]+)_([a-z0-9-]+)_recovery_", re.IGNORECASE)

ProgressCallback = Callable[[int, int, int], None]
StatusCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PreparedImage:
    source_path: Path
    member_name: str
    image_size: int
    sha1: str
    title: str
    subtitle: str
    is_local: bool = False


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(Exception):
    pass


class VerificationError(DownloadError):
    pass


class DownloadState(StrEnum):
    NONE = "none"
    RESUMABLE = "resumable"
    COMPLETE = "complete"


def aria2_binary() -> str | None:
    return shutil.which("aria2c")


def _load_ctl(ctl_path: Path, release: Release, part_path: Path) -> dict[str, Any] | None:
    if not ctl_path.is_file() or not part_path.is_file():
        return None
    try:
        data = json.loads(ctl_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        if data.get("url") != release.url or data.get("total") != release.zip_size:
            return None
        committed = data.get("committed")
        if not isinstance(committed, int) or isinstance(committed, bool):
            return None
        actual = part_path.stat().st_size
        if committed < 0 or committed > release.zip_size or actual < committed:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _write_ctl(ctl_path: Path, payload: dict[str, Any]) -> None:
    tmp = ctl_path.with_name(f"{ctl_path.name}.tmp")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, ctl_path)


def inspect_existing_download(
    dest_dir: Path,
    release: Release,
    *,
    allow_aria2: bool = True,
) -> tuple[DownloadState, int]:
    final_path = dest_dir / release.archive_name
    aria_ctl = dest_dir / f"{release.archive_name}.aria2"
    part_path = dest_dir / f"{release.archive_name}.part"
    ctl_path = dest_dir / f"{release.archive_name}.part.json"

    if final_path.is_file() and not aria_ctl.exists():
        try:
            if final_path.stat().st_size == release.zip_size:
                return DownloadState.COMPLETE, release.zip_size
        except OSError:
            pass

    if allow_aria2 and aria2_binary() and aria_ctl.is_file() and final_path.is_file():
        return DownloadState.RESUMABLE, 0

    ctl = _load_ctl(ctl_path, release, part_path)
    if ctl is not None and ctl["committed"] > 0:
        return DownloadState.RESUMABLE, int(ctl["committed"])

    return DownloadState.NONE, 0


def check_free_space(dest_dir: Path, release: Release) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = 0
    for candidate in (
        dest_dir / release.archive_name,
        dest_dir / f"{release.archive_name}.part",
    ):
        if candidate.is_file():
            try:
                existing = max(existing, min(candidate.stat().st_size, release.zip_size))
            except OSError:
                pass
    remaining = max(0, release.zip_size - existing) + SPACE_MARGIN
    free = shutil.disk_usage(dest_dir).free
    if free < remaining:
        raise DownloadError(
            f"Not enough disk space ({format_bytes(free)} available, {format_bytes(remaining)} required)."
        )


class BaseDownloader:
    def __init__(
        self,
        release: Release,
        dest_dir: Path,
        *,
        progress_cb: ProgressCallback | None = None,
        status_cb: StatusCallback | None = None,
    ) -> None:
        self.release = release
        self.dest_dir = dest_dir
        self._progress_cb = progress_cb
        self._status_cb = status_cb
        self._cancel_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()
        self._resume_event.set()

    def run(self) -> Path:
        raise NotImplementedError

    def _emit_progress(self, done: int, total: int, bps: int) -> None:
        if self._progress_cb is not None:
            self._progress_cb(done, total, bps)

    def _emit_status(self, message: str) -> None:
        if self._status_cb is not None:
            self._status_cb(message)


class HttpDownloader(BaseDownloader):
    def __init__(
        self,
        release: Release,
        dest_dir: Path,
        *,
        progress_cb: ProgressCallback | None = None,
        status_cb: StatusCallback | None = None,
        max_retries: int = 0,
        commit_interval: int = COMMIT_INTERVAL,
    ) -> None:
        super().__init__(release, dest_dir, progress_cb=progress_cb, status_cb=status_cb)
        self._max_retries = max_retries
        self._commit_interval = commit_interval

    def _ssl_context(self) -> ssl.SSLContext | None:
        parsed = urlparse(self.release.url)
        if parsed.scheme == "https":
            return ssl.create_default_context()
        if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
            return None
        raise DownloadError(f"Unsupported URL scheme: {parsed.scheme!r}")

    def run(self) -> Path:
        check_free_space(self.dest_dir, self.release)
        final_path = self.dest_dir / self.release.archive_name
        aria_ctl = self.dest_dir / f"{self.release.archive_name}.aria2"
        part_path = self.dest_dir / f"{self.release.archive_name}.part"
        ctl_path = self.dest_dir / f"{self.release.archive_name}.part.json"

        # Never resume a foreign aria2 sparse allocation without aria2's piece bitmap.
        if aria_ctl.exists():
            for p in (final_path, aria_ctl):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

        if final_path.is_file() and final_path.stat().st_size == self.release.zip_size:
            self._emit_progress(self.release.zip_size, self.release.zip_size, 0)
            return final_path

        retries = 0
        ctx = self._ssl_context()

        while True:
            if self._cancel_event.is_set():
                raise DownloadCancelled()

            if not self._resume_event.is_set():
                self._emit_status("Paused")
                while not self._resume_event.wait(timeout=0.2):
                    if self._cancel_event.is_set():
                        raise DownloadCancelled()
                if self._cancel_event.is_set():
                    raise DownloadCancelled()
                self._emit_status("")

            ctl = _load_ctl(ctl_path, self.release, part_path)
            if ctl is None:
                committed = 0
                etag = ""
                last_modified = ""
            else:
                committed = int(ctl["committed"])
                etag = str(ctl.get("etag") or "")
                last_modified = str(ctl.get("last_modified") or "")

            try:
                committed, etag, last_modified = self._transfer_once(
                    part_path=part_path,
                    ctl_path=ctl_path,
                    committed=committed,
                    etag=etag,
                    last_modified=last_modified,
                    ctx=ctx,
                )
                retries = 0
                if committed >= self.release.zip_size:
                    break
            except DownloadCancelled:
                raise
            except HTTPError as exc:
                if exc.code == 416:
                    committed = 0
                    etag = ""
                    last_modified = ""
                    part_path.unlink(missing_ok=True)
                    ctl_path.unlink(missing_ok=True)
                    continue
                if exc.code in (400, 401, 403, 404):
                    raise DownloadError(f"Download failed (HTTP {exc.code}).") from exc
                retries += 1
                self._wait_retry(retries, exc)
            except (URLError, TimeoutError, OSError) as exc:
                retries += 1
                self._wait_retry(retries, exc)

        if not part_path.is_file() or part_path.stat().st_size != self.release.zip_size:
            raise DownloadError("Downloaded archive size does not match manifest.")

        final_path.unlink(missing_ok=True)
        os.replace(part_path, final_path)
        ctl_path.unlink(missing_ok=True)
        self._emit_progress(self.release.zip_size, self.release.zip_size, 0)
        return final_path

    def _wait_retry(self, retries: int, exc: Exception) -> None:
        if self._max_retries > 0 and retries > self._max_retries:
            raise DownloadError(f"Download failed after {self._max_retries} retries: {exc}") from exc
        delay = min(30.0, float(2 ** min(retries - 1, 5)))
        log.warning("Download interrupted (%s); retrying in %.0fs", exc, delay)
        self._emit_status("Waiting for network…")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self._cancel_event.wait(timeout=0.25):
                raise DownloadCancelled()
        self._emit_status("")

    def _transfer_once(
        self,
        *,
        part_path: Path,
        ctl_path: Path,
        committed: int,
        etag: str,
        last_modified: str,
        ctx: ssl.SSLContext | None,
    ) -> tuple[int, str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if committed > 0 and part_path.is_file():
            headers["Range"] = f"bytes={committed}-"
            validator = etag or last_modified
            if validator:
                headers["If-Range"] = validator

        req = Request(self.release.url, headers=headers)
        with urlopen(req, timeout=20.0, context=ctx) as resp:
            status = resp.status
            resp_etag = resp.headers.get("ETag") or etag
            resp_lm = resp.headers.get("Last-Modified") or last_modified

            if status == 206 and committed > 0:
                cr = resp.headers.get("Content-Range") or ""
                m = _CONTENT_RANGE_RE.match(cr.strip())
                if not m or int(m.group(1)) != committed:
                    raise DownloadError(f"Invalid Content-Range header: {cr!r}")
                if m.group(3) != "*" and int(m.group(3)) != self.release.zip_size:
                    raise DownloadError("Remote archive size changed cleanly outside manifest.")
                mode = "r+b"
            elif status == 200:
                committed = 0
                resp_etag = resp.headers.get("ETag") or ""
                resp_lm = resp.headers.get("Last-Modified") or ""
                cl = resp.headers.get("Content-Length")
                if cl is not None and int(cl) != self.release.zip_size:
                    raise DownloadError("Remote archive Content-Length does not match manifest.")
                mode = "wb"
            else:
                raise DownloadError(f"Unexpected HTTP status {status}")

            with open(part_path, mode) as fh:
                if committed > 0:
                    fh.seek(committed)
                    fh.truncate(committed)
                _write_ctl(
                    ctl_path,
                    {
                        "v": 1,
                        "url": self.release.url,
                        "etag": resp_etag,
                        "last_modified": resp_lm,
                        "total": self.release.zip_size,
                        "committed": committed,
                    },
                )

                written = committed
                last_commit = committed
                win_start = time.monotonic()
                win_bytes = 0
                bps = 0
                last_ui = 0.0

                def commit_now() -> None:
                    nonlocal committed, last_commit
                    fh.flush()
                    os.fsync(fh.fileno())
                    committed = written
                    last_commit = written
                    _write_ctl(
                        ctl_path,
                        {
                            "v": 1,
                            "url": self.release.url,
                            "etag": resp_etag,
                            "last_modified": resp_lm,
                            "total": self.release.zip_size,
                            "committed": committed,
                        },
                    )

                self._emit_progress(written, self.release.zip_size, 0)

                while True:
                    if self._cancel_event.is_set():
                        commit_now()
                        raise DownloadCancelled()
                    if not self._resume_event.is_set():
                        commit_now()
                        return committed, resp_etag, resp_lm

                    chunk = resp.read(READ_CHUNK)
                    if not chunk:
                        break
                    if written + len(chunk) > self.release.zip_size:
                        raise DownloadError("Server sent more bytes than declared zip size.")
                    fh.write(chunk)
                    written += len(chunk)
                    win_bytes += len(chunk)

                    if written - last_commit >= self._commit_interval:
                        commit_now()

                    now = time.monotonic()
                    elapsed = now - win_start
                    if elapsed >= 0.5:
                        inst = int(win_bytes / elapsed)
                        bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
                        win_start = now
                        win_bytes = 0
                    if now - last_ui >= 0.12:
                        last_ui = now
                        self._emit_progress(written, self.release.zip_size, bps)

                commit_now()
                if written < self.release.zip_size:
                    raise OSError(f"Connection closed early ({written}/{self.release.zip_size} bytes)")
                return committed, resp_etag, resp_lm


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _aria2_alloc_mode() -> str:
    if sys.platform.startswith("linux"):
        return "falloc"
    if sys.platform == "win32":
        return "prealloc"
    return "none"


class Aria2Downloader(BaseDownloader):
    def __init__(
        self,
        release: Release,
        dest_dir: Path,
        *,
        progress_cb: ProgressCallback | None = None,
        status_cb: StatusCallback | None = None,
        max_tries: int = 0,
    ) -> None:
        super().__init__(release, dest_dir, progress_cb=progress_cb, status_cb=status_cb)
        self._max_tries = max_tries

    @staticmethod
    def _rpc(port: int, secret: str, method: str, params: list[Any] | None = None) -> Any:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "fk",
                "method": method,
                "params": [f"token:{secret}", *(params or [])],
            }
        ).encode("utf-8")
        req = Request(
            f"http://127.0.0.1:{port}/jsonrpc",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if "error" in payload:
            raise DownloadError(f"aria2 RPC error: {payload['error']}")
        return payload.get("result")

    def run(self) -> Path:
        exe = aria2_binary()
        if not exe:
            raise DownloadError("aria2c is not installed")

        check_free_space(self.dest_dir, self.release)
        final_path = self.dest_dir / self.release.archive_name
        aria_ctl = self.dest_dir / f"{self.release.archive_name}.aria2"

        if final_path.is_file() and not aria_ctl.exists():
            if final_path.stat().st_size == self.release.zip_size:
                self._emit_progress(self.release.zip_size, self.release.zip_size, 0)
                return final_path
            final_path.unlink(missing_ok=True)

        port = _free_local_port()
        secret = secrets.token_hex(32)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        cmd = [
            exe,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            f"--stop-with-process={os.getpid()}",
            "--continue=true",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--check-integrity=false",
            "--max-connection-per-server=8",
            "--split=8",
            "--min-split-size=16M",
            f"--max-tries={self._max_tries}",
            "--retry-wait=3",
            f"--file-allocation={_aria2_alloc_mode()}",
            f"--user-agent={USER_AGENT}",
            f"--dir={self.dest_dir}",
            "--quiet=true",
        ]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        gid: str | None = None
        paused_in_rpc = False
        try:
            ready = False
            for _ in range(40):
                if proc.poll() is not None:
                    break
                try:
                    self._rpc(port, secret, "aria2.getVersion")
                    ready = True
                    break
                except (URLError, OSError, DownloadError):
                    time.sleep(0.1)
            if not ready:
                raise DownloadError("Failed to initialize aria2c RPC daemon.")

            gid = str(
                self._rpc(
                    port,
                    secret,
                    "aria2.addUri",
                    [[self.release.url], {"out": self.release.archive_name}],
                )
            )

            while True:
                if self._cancel_event.is_set():
                    try:
                        self._rpc(port, secret, "aria2.pause", [gid])
                        self._rpc(port, secret, "aria2.shutdown")
                    except (URLError, OSError, DownloadError):
                        pass
                    proc.wait(timeout=5.0)
                    raise DownloadCancelled()

                want_paused = not self._resume_event.is_set()
                if want_paused and not paused_in_rpc:
                    self._rpc(port, secret, "aria2.pause", [gid])
                    paused_in_rpc = True
                    self._emit_status("Paused")
                elif not want_paused and paused_in_rpc:
                    self._rpc(port, secret, "aria2.unpause", [gid])
                    paused_in_rpc = False
                    self._emit_status("")

                info = self._rpc(
                    port,
                    secret,
                    "aria2.tellStatus",
                    [
                        gid,
                        [
                            "status",
                            "completedLength",
                            "totalLength",
                            "downloadSpeed",
                            "errorCode",
                            "errorMessage",
                        ],
                    ],
                )
                status = info.get("status")
                done = int(info.get("completedLength") or 0)
                total = int(info.get("totalLength") or self.release.zip_size) or self.release.zip_size
                speed = int(info.get("downloadSpeed") or 0)
                self._emit_progress(done, total, speed)

                if status == "complete":
                    break
                if status == "error":
                    msg = info.get("errorMessage") or f"code {info.get('errorCode')}"
                    raise DownloadError(f"aria2 download failed: {msg}")
                time.sleep(0.35)

            try:
                self._rpc(port, secret, "aria2.shutdown")
            except (URLError, OSError, DownloadError):
                pass
            proc.wait(timeout=5.0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if not final_path.is_file() or aria_ctl.exists() or final_path.stat().st_size != self.release.zip_size:
            raise DownloadError("Downloaded archive from aria2 is incomplete.")

        for stale in (
            self.dest_dir / f"{self.release.archive_name}.part",
            self.dest_dir / f"{self.release.archive_name}.part.json",
        ):
            stale.unlink(missing_ok=True)

        self._emit_progress(self.release.zip_size, self.release.zip_size, 0)
        return final_path


def create_downloader(
    release: Release,
    dest_dir: Path,
    *,
    use_aria2: bool = True,
    progress_cb: ProgressCallback | None = None,
    status_cb: StatusCallback | None = None,
) -> BaseDownloader:
    if use_aria2 and aria2_binary():
        return Aria2Downloader(release, dest_dir, progress_cb=progress_cb, status_cb=status_cb)
    return HttpDownloader(release, dest_dir, progress_cb=progress_cb, status_cb=status_cb)


def _stream_digests(
    stream: Any,
    expected_size: int,
    *,
    compute_md5: bool,
    progress_cb: Callable[[int, int], None] | None = None,
    metrics_cb: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    md5 = hashlib.md5() if compute_md5 else None
    done = 0
    win_start = time.monotonic()
    win_bytes = 0
    bps = 0
    last_ui = 0.0

    if progress_cb is not None:
        progress_cb(0, expected_size)
    if metrics_cb is not None:
        metrics_cb(0, expected_size, 0)

    while True:
        if cancel_check is not None and cancel_check():
            raise DownloadCancelled()
        chunk = stream.read(VERIFY_CHUNK)
        if not chunk:
            break
        sha1.update(chunk)
        if md5 is not None:
            md5.update(chunk)
        n = len(chunk)
        done += n
        win_bytes += n

        now = time.monotonic()
        elapsed = now - win_start
        if elapsed >= 0.4:
            inst = int(win_bytes / elapsed)
            bps = inst if bps == 0 else int(bps * 0.6 + inst * 0.4)
            win_start = now
            win_bytes = 0
        if progress_cb is not None and (done == expected_size or (now - last_ui) >= 0.1):
            progress_cb(done, expected_size)
        if metrics_cb is not None and (done == expected_size or (now - last_ui) >= 0.1):
            metrics_cb(done, expected_size, bps)
        if done == expected_size or (now - last_ui) >= 0.1:
            last_ui = now

    if done != expected_size:
        raise VerificationError("Truncated image stream.")
    return sha1.hexdigest().lower(), (md5.hexdigest().lower() if md5 is not None else "")


def _check_not_foreign_board(filename: str) -> str | None:
    m = _RECOVERY_BOARD_RE.match(Path(filename).name)
    if not m:
        return None
    ver, board = m.group(1), m.group(2)
    if board.lower() != "reven":
        raise VerificationError(
            f"This recovery image is for '{board}', not ChromeOS Flex (reven)."
        )
    return ver


def _release_subtitle(release: Release) -> str:
    chrome_major = release.chrome_version.split(".")[0] if release.chrome_version else ""
    return f"{release.channel.label} · Chrome {chrome_major}" if chrome_major else release.channel.label


def verify_archive(
    zip_path: Path,
    release: Release,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    metrics_cb: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> PreparedImage:
    if not zip_path.is_file():
        raise VerificationError("Archive file does not exist.")
    if zip_path.stat().st_size != release.zip_size:
        raise VerificationError("Archive file size does not match Google recovery manifest.")

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            if len(members) != 1:
                raise VerificationError(f"Expected 1 file inside archive, found {len(members)}.")
            info = members[0]
            if info.filename != release.file:
                raise VerificationError(f"Unexpected image filename inside archive: {info.filename!r}")
            if info.file_size != release.image_size:
                raise VerificationError("Uncompressed image size does not match manifest.")
    except zipfile.BadZipFile as exc:
        raise VerificationError(f"Corrupt ZIP archive: {exc}") from exc

    with open(zip_path, "rb") as zip_fh:
        sha1_hex, md5_hex = _stream_digests(
            zip_fh,
            release.zip_size,
            compute_md5=True,
            progress_cb=progress_cb,
            metrics_cb=metrics_cb,
            cancel_check=cancel_check,
        )

    if sha1_hex != release.sha1:
        raise VerificationError("SHA-1 checksum mismatch.")
    if md5_hex != release.md5:
        raise VerificationError("MD5 checksum mismatch.")

    return PreparedImage(
        source_path=zip_path,
        member_name=release.file,
        image_size=release.image_size,
        sha1="",
        title=f"ChromeOS Flex {release.version}",
        subtitle=_release_subtitle(release),
        is_local=False,
    )


def prepare_local_image(
    image_path: Path,
    catalog: list[Release] | tuple[Release, ...] = (),
    *,
    metrics_cb: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> PreparedImage:
    if not image_path.is_file():
        raise VerificationError("Selected file does not exist.")
    suffix = image_path.suffix.lower()
    if suffix in (".part", ".aria2", ".json"):
        raise VerificationError("Incomplete download file cannot be used directly.")

    file_size = image_path.stat().st_size
    if file_size <= 0:
        raise VerificationError("Selected image file is empty.")

    outer_ver = _check_not_foreign_board(image_path.name)

    if suffix == ".zip" or zipfile.is_zipfile(image_path):
        try:
            with zipfile.ZipFile(image_path, "r") as zf:
                candidates = [
                    m
                    for m in zf.infolist()
                    if not m.is_dir() and not m.filename.startswith("__MACOSX/")
                ]
                if len(candidates) != 1:
                    raise VerificationError(
                        f"Expected 1 disk image inside ZIP archive, found {len(candidates)}."
                    )
                info = candidates[0]
                if ".." in info.filename or info.filename.startswith("/"):
                    raise VerificationError("Unsafe member path inside ZIP archive.")
                if info.file_size <= 0:
                    raise VerificationError("Image inside ZIP archive is empty.")

                inner_ver = _check_not_foreign_board(info.filename)
                with zf.open(info, "r") as probe:
                    if not probe.read(64):
                        raise VerificationError("Image inside ZIP archive is unreadable.")
        except zipfile.BadZipFile as exc:
            raise VerificationError(f"Invalid ZIP archive: {exc}") from exc
        except zlib_error() as exc:
            raise VerificationError(f"Archive decompression error: {exc}") from exc

        matched = next(
            (r for r in catalog if r.file == info.filename and r.image_size == info.file_size),
            None,
        )
        if matched is not None:
            if file_size != matched.zip_size:
                raise VerificationError("Archive file size does not match official ChromeOS Flex manifest.")
            with open(image_path, "rb") as zip_fh:
                sha1_hex, md5_hex = _stream_digests(
                    zip_fh,
                    file_size,
                    compute_md5=True,
                    metrics_cb=metrics_cb,
                    cancel_check=cancel_check,
                )
            if sha1_hex != matched.sha1 or md5_hex != matched.md5:
                raise VerificationError("Archive checksum does not match official ChromeOS Flex manifest.")
            title = f"ChromeOS Flex {matched.version}"
            subtitle = _release_subtitle(matched)
        else:
            if metrics_cb is not None:
                metrics_cb(file_size, file_size, 0)
            ver = inner_ver or outer_ver
            title = f"ChromeOS Flex {ver}" if ver else Path(info.filename).name
            subtitle = "Local image"

        return PreparedImage(
            source_path=image_path,
            member_name=info.filename,
            image_size=info.file_size,
            sha1="",
            title=title,
            subtitle=subtitle,
            is_local=True,
        )

    matched_raw = next((r for r in catalog if r.file == image_path.name), None)
    if matched_raw is not None:
        if file_size != matched_raw.image_size:
            raise VerificationError("Image file size does not match official ChromeOS Flex manifest.")
        title = f"ChromeOS Flex {matched_raw.version}"
        subtitle = _release_subtitle(matched_raw)
    else:
        title = f"ChromeOS Flex {outer_ver}" if outer_ver else image_path.name
        subtitle = "Local image"

    if metrics_cb is not None:
        metrics_cb(file_size, file_size, 0)

    return PreparedImage(
        source_path=image_path,
        member_name="",
        image_size=file_size,
        sha1="",
        title=title,
        subtitle=subtitle,
        is_local=True,
    )


def zlib_error() -> type[Exception]:
    import zlib

    return zlib.error
