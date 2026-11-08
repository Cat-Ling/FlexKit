from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import logging
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flexkit import USER_AGENT, user_cache_dir

log = logging.getLogger("flexkit.catalog")

PRIMARY_CATALOG_URL = "https://dl.google.com/dl/edgedl/chromeos/recovery/cloudready_recovery2.json"
FALLBACK_CATALOG_URL = "https://dl.google.com/dl/edgedl/chromeos/recovery/cloudready_recovery.json"

_REVEN_FILE_RE = re.compile(r"^chromeos_[0-9.]+_reven_recovery_[a-z0-9-]+_[A-Za-z0-9._-]+\.bin$")
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class CatalogError(RuntimeError):
    pass


class Channel(StrEnum):
    STABLE = "STABLE"
    LTS = "LTR"
    LTC = "LTC"
    BETA = "BETA"
    DEV = "DEV"

    @property
    def label(self) -> str:
        return {
            Channel.STABLE: "Stable",
            Channel.LTS: "LTS",
            Channel.LTC: "LTC",
            Channel.BETA: "Beta",
            Channel.DEV: "Dev",
        }[self]


CHANNEL_ORDER: tuple[Channel, ...] = (
    Channel.STABLE,
    Channel.LTS,
    Channel.LTC,
    Channel.BETA,
    Channel.DEV,
)


@dataclass(frozen=True, slots=True)
class Release:
    channel: Channel
    version: str
    chrome_version: str
    file: str
    url: str
    zip_size: int
    image_size: int
    sha1: str
    md5: str

    @property
    def archive_name(self) -> str:
        return f"{self.file}.zip"


def _is_positive_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val > 0


def _validate_entry(raw: Any) -> Release | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("manufacturer") != "Google ChromeOS Flex":
        return None
    hwidmatch = raw.get("hwidmatch")
    if not isinstance(hwidmatch, str) or not hwidmatch.startswith("^REVEN"):
        return None

    file_name = raw.get("file")
    if not isinstance(file_name, str) or ".." in file_name or not _REVEN_FILE_RE.match(file_name):
        return None

    url = raw.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    expected_path = f"/dl/edgedl/chromeos/recovery/{file_name}.zip"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "dl.google.com"
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return None

    zip_size = raw.get("zipfilesize")
    image_size = raw.get("filesize")
    if not (_is_positive_int(zip_size) and _is_positive_int(image_size)) or image_size < zip_size:
        return None

    sha1 = raw.get("sha1")
    md5 = raw.get("md5")
    if not (isinstance(sha1, str) and _SHA1_RE.match(sha1)):
        return None
    if not (isinstance(md5, str) and _MD5_RE.match(md5)):
        return None

    channel_raw = raw.get("channel")
    if not isinstance(channel_raw, str):
        return None
    try:
        channel = Channel(channel_raw.upper())
    except ValueError:
        return None

    version = str(raw.get("version") or "").strip()
    if not version:
        return None
    chrome_version = str(raw.get("chrome_version") or "").strip()

    return Release(
        channel=channel,
        version=version,
        chrome_version=chrome_version,
        file=file_name,
        url=url,
        zip_size=zip_size,
        image_size=image_size,
        sha1=sha1.lower(),
        md5=md5.lower(),
    )


def parse_catalog(payload: str | bytes) -> list[Release]:
    try:
        items = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CatalogError("Catalog payload is not valid JSON") from exc

    if not isinstance(items, list):
        raise CatalogError("Catalog root must be a JSON list")

    by_channel: dict[Channel, Release] = {}
    for entry in items:
        rel = _validate_entry(entry)
        if rel is not None and rel.channel not in by_channel:
            by_channel[rel.channel] = rel

    if not by_channel:
        raise CatalogError("No valid ChromeOS Flex releases found in catalog")

    return [by_channel[ch] for ch in CHANNEL_ORDER if ch in by_channel]


def _http_get(url: str, timeout: float) -> bytes:
    ctx = ssl.create_default_context()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        if resp.status != 200:
            raise CatalogError(f"HTTP {resp.status} fetching {url}")
        return resp.read(4 * 1024 * 1024)


def fetch_catalog(
    *,
    urls: tuple[str, ...] = (PRIMARY_CATALOG_URL, FALLBACK_CATALOG_URL),
    cache_dir: Path | None = None,
    timeout: float = 15.0,
) -> tuple[list[Release], bool]:
    resolved_cache = (cache_dir or user_cache_dir()) / "catalog.json"
    last_err: Exception | None = None

    for url in urls:
        try:
            raw = _http_get(url, timeout=timeout)
            releases = parse_catalog(raw)
            try:
                resolved_cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = resolved_cache.with_suffix(".json.tmp")
                tmp.write_bytes(raw)
                tmp.replace(resolved_cache)
            except OSError as cache_err:
                log.warning("Could not persist catalog cache: %s", cache_err)
            return releases, False
        except (URLError, OSError, CatalogError) as exc:
            log.warning("Catalog fetch failed for %s: %s", url, exc)
            last_err = exc

    if resolved_cache.is_file():
        try:
            releases = parse_catalog(resolved_cache.read_bytes())
            return releases, True
        except (OSError, CatalogError) as exc:
            log.warning("Cached catalog unusable: %s", exc)

    raise CatalogError(f"Unable to load ChromeOS Flex catalog: {last_err}")


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1000:
        return f"{num_bytes} B"
    if num_bytes < 1_000_000:
        return f"{num_bytes / 1_000:.1f} KB"
    if num_bytes < 1_000_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes < 1_000_000_000_000:
        val = num_bytes / 1_000_000_000
        return f"{val:.2f} GB" if val < 10 else f"{val:.1f} GB"
    return f"{num_bytes / 1_000_000_000_000:.2f} TB"
