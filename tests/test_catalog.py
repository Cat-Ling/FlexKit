from __future__ import annotations

import copy
import json
from pathlib import Path
import pytest

from flexkit.catalog import (
    CHANNEL_ORDER,
    CatalogError,
    Channel,
    fetch_catalog,
    format_bytes,
    parse_catalog,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cloudready_recovery2.json"


def test_parse_real_flex_catalog() -> None:
    raw = FIXTURE.read_bytes()
    releases = parse_catalog(raw)

    assert [r.channel for r in releases] == list(CHANNEL_ORDER)
    assert [r.channel.label for r in releases] == ["Stable", "LTS", "LTC", "Beta", "Dev"]

    stable = releases[0]
    assert stable.channel == Channel.STABLE
    assert stable.version == "16836.30.0"
    assert stable.chrome_version == "156.0.8078.77"
    assert stable.zip_size == 1376548043
    assert stable.image_size == 9523261952
    assert stable.sha1 == "978f3920026eb25c1b3bdea9e93541033d64084d"
    assert stable.md5 == "5121ab6cef9a644b4d94b45e28f00ca4"
    assert stable.archive_name.endswith(".bin.zip")


def test_rejects_chromebook_recovery_entry() -> None:
    chromebook_entry = [
        {
            "channel": "STABLE",
            "file": "chromeos_16700.68.0_brya_recovery_stable-channel_BryaMPKeys-v23.bin",
            "filesize": 5170026496,
            "hwidmatch": "^TAEKO-XLTF.*",
            "manufacturer": "Lenovo",
            "md5": "e6b3c9f15cb36f1dcfe97f73ec7772d5",
            "model": 'IdeaPad Flex 5i Chromebook Plus (14", 7)',
            "name": 'IdeaPad Flex 5i Chromebook Plus (14", 7)',
            "sha1": "c9e94c3b43d80cee05fc22778367c508a7228bf3",
            "url": "https://dl.google.com/dl/edgedl/chromeos/recovery/chromeos_16700.68.0_brya_recovery_stable-channel_BryaMPKeys-v23.bin.zip",
            "version": "16700.68.0",
            "zipfilesize": 2800668532,
        }
    ]
    with pytest.raises(CatalogError):
        parse_catalog(json.dumps(chromebook_entry))


@pytest.mark.parametrize(
    "mutation",
    [
        {"url": "http://dl.google.com/dl/edgedl/chromeos/recovery/chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin.zip"},
        {"url": "https://evil.example.com/dl/edgedl/chromeos/recovery/chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin.zip"},
        {"hwidmatch": "^ATLAS.*"},
        {"manufacturer": "Neverware"},
        {"file": "../chromeos_16836.30.0_reven_recovery_stable-channel_RevenMPKeys-v11.bin"},
        {"sha1": "not-a-sha1"},
        {"filesize": 100, "zipfilesize": 500},
    ],
)
def test_rejects_tampered_entries(mutation: dict[str, object]) -> None:
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))[-1]
    tampered = copy.deepcopy(base)
    tampered.update(mutation)
    with pytest.raises(CatalogError):
        parse_catalog(json.dumps([tampered]))


def test_offline_cache_fallback(tmp_path: Path) -> None:
    cache_file = tmp_path / "catalog.json"
    cache_file.write_bytes(FIXTURE.read_bytes())

    releases, from_cache = fetch_catalog(
        urls=("https://127.0.0.1:1/unreachable.json",),
        cache_dir=tmp_path,
        timeout=0.3,
    )
    assert from_cache is True
    assert releases[0].version == "16836.30.0"


def test_format_bytes() -> None:
    assert format_bytes(1376548043) == "1.38 GB"
    assert format_bytes(9523261952) == "9.52 GB"
    assert format_bytes(256060514304) == "256.1 GB"
