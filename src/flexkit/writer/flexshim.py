from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re
import struct
from typing import Protocol

from flexkit import assets_dir
from flexkit.writer import WriteError

_SECTOR_SIZE = 512
_ATTR_VOLUME_ID = 0x08
_ATTR_DIR = 0x10
_ATTR_ARCHIVE = 0x20
_ATTR_LFN = 0x0F
_DOS_DEFAULT_TIME = 0x0000
_DOS_DEFAULT_DATE = 0x5C21  # 2026-01-01
_CROS_DEBUG_RE = re.compile(r"^\s*#?\s*cros_debug\s*=")
_VALID_83_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


class _BlockIO(Protocol):
    def read_at(self, offset: int, size: int) -> bytes: ...

    def write_at(self, offset: int, data: bytes | memoryview) -> None: ...


@dataclass(frozen=True, slots=True)
class _DirEntry:
    name: str
    short_name: bytes
    attr: int
    first_cluster: int
    size: int
    entry_index: int


def load_flexshim_efi() -> bytes:
    repo_flexshim = assets_dir().parents[2] / "flexshim"
    candidates = (
        assets_dir() / "flexshim" / "flexshimx64.efi",
        repo_flexshim / "target" / "x86_64-unknown-uefi" / "release" / "flexshimx64.efi",
        repo_flexshim / "build" / "flexshimx64.efi",
    )
    for candidate in candidates:
        if candidate.is_file():
            data = candidate.read_bytes()
            if len(data) >= 64 and data[:2] == b"MZ":
                return data
    raise WriteError("Bundled flexshimx64.efi not found or invalid; rebuild flexshim.")


def load_flexshim_cfg_template() -> str:
    repo_flexshim = assets_dir().parents[2] / "flexshim"
    candidates = (
        assets_dir() / "flexshim" / "flexshim.cfg",
        repo_flexshim / "flexshim.cfg",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise WriteError("Bundled flexshim.cfg template not found.")


def render_flexshim_cfg(*, cros_debug: bool, template: str | None = None) -> bytes:
    src = template if template is not None else load_flexshim_cfg_template()
    target_line = "cros_debug=true" if cros_debug else "# cros_debug=true"
    out_lines: list[str] = []
    replaced = False

    for line in src.splitlines():
        if _CROS_DEBUG_RE.match(line):
            if not replaced:
                out_lines.append(target_line)
                replaced = True
        else:
            out_lines.append(line)

    if not replaced and cros_debug:
        out_lines.append(target_line)

    return ("\n".join(out_lines) + "\n").encode("utf-8")


def _lfn_checksum(short11: bytes) -> int:
    total = 0
    for b in short11:
        total = (((total & 1) << 7) + (total >> 1) + b) & 0xFF
    return total


def _format_short_name(short11: bytes, ntres: int = 0) -> str:
    stem = short11[:8].rstrip(b" ").decode("ascii", errors="ignore")
    ext = short11[8:11].rstrip(b" ").decode("ascii", errors="ignore")
    if ntres & 0x08:
        stem = stem.lower()
    if ntres & 0x10:
        ext = ext.lower()
    return f"{stem}.{ext}" if ext else stem


def _sanitize_83_part(text: str) -> str:
    out: list[str] = []
    for ch in text.upper():
        if ch in _VALID_83_CHARS:
            out.append(ch)
        elif ch not in (" ", "."):
            out.append("_")
    return "".join(out)


def _needs_lfn(filename: str) -> bool:
    if filename != filename.upper():
        return True
    if "." not in filename:
        stem, ext = filename, ""
    else:
        stem, ext = filename.rsplit(".", 1)
    if "." in stem or not stem or len(stem) > 8 or len(ext) > 3:
        return True
    return any(ch not in _VALID_83_CHARS for ch in stem + ext)


def _make_short_name(filename: str, existing_shorts: set[bytes]) -> bytes:
    if "." in filename:
        raw_stem, raw_ext = filename.rsplit(".", 1)
    else:
        raw_stem, raw_ext = filename, ""
    stem = _sanitize_83_part(raw_stem)
    ext = _sanitize_83_part(raw_ext)[:3]
    if not stem:
        raise WriteError(f"Invalid FAT filename: {filename!r}")

    if not _needs_lfn(filename.upper()):
        cand = stem.encode("ascii").ljust(8, b" ") + ext.encode("ascii").ljust(3, b" ")
        if cand not in existing_shorts:
            return cand

    for idx in range(1, 10000):
        suffix = f"~{idx}"
        prefix = stem[: 8 - len(suffix)]
        cand_stem = (prefix + suffix).encode("ascii").ljust(8, b" ")
        cand = cand_stem + ext.encode("ascii").ljust(3, b" ")
        if cand not in existing_shorts:
            return cand
    raise WriteError(f"Could not allocate unique 8.3 short name for {filename!r}")


def _encode_lfn_entries(filename: str, checksum: int) -> list[bytes]:
    units = [u for u in filename.encode("utf-16le")]
    code_units = [int.from_bytes(bytes(units[i : i + 2]), "little") for i in range(0, len(units), 2)]
    n_entries = max(1, (len(code_units) + 12) // 13)
    padded = list(code_units)
    target_units = n_entries * 13
    if len(padded) < target_units:
        padded.append(0x0000)
        while len(padded) < target_units:
            padded.append(0xFFFF)

    records: list[bytes] = []
    for order in range(n_entries, 0, -1):
        chunk = padded[(order - 1) * 13 : order * 13]
        raw_u16 = b"".join(struct.pack("<H", u) for u in chunk)
        seq = order | (0x40 if order == n_entries else 0)
        rec = struct.pack(
            "<B10sBBB12sH4s",
            seq,
            raw_u16[0:10],
            _ATTR_LFN,
            0x00,
            checksum,
            raw_u16[10:22],
            0,
            raw_u16[22:26],
        )
        records.append(rec)
    return records


def _parse_directory(buf: bytes | bytearray, fat_bits: int) -> tuple[list[_DirEntry], set[bytes], int]:
    entries: list[_DirEntry] = []
    shorts: set[bytes] = set()
    pending_lfn: list[tuple[int, int, bytes]] = []
    end_index = len(buf) // 32

    for idx in range(end_index):
        ent = buf[idx * 32 : (idx + 1) * 32]
        first = ent[0]
        if first == 0x00:
            end_index = idx
            break
        if first == 0xE5:
            pending_lfn.clear()
            continue

        attr = ent[11]
        if attr == _ATTR_LFN:
            lfn_type = ent[12]
            if lfn_type != 0:
                pending_lfn.clear()
                continue
            seq = ent[0]
            csum = ent[13]
            chars = bytes(ent[1:11]) + bytes(ent[14:26]) + bytes(ent[28:32])
            pending_lfn.append((seq, csum, chars))
            continue

        if attr & _ATTR_VOLUME_ID:
            pending_lfn.clear()
            continue

        short11 = bytes(ent[0:11])
        shorts.add(short11)
        short_name = _format_short_name(short11, ent[12])
        name = short_name

        if pending_lfn:
            expected_csum = _lfn_checksum(short11)
            count = len(pending_lfn)
            valid = all(
                cs == expected_csum and (sq & 0x1F) == (count - pos) and ((sq & 0x40 != 0) == (pos == 0))
                for pos, (sq, cs, _) in enumerate(pending_lfn)
            )
            if valid:
                combined = b"".join(parts for _, _, parts in reversed(pending_lfn))
                decoded = combined.decode("utf-16le", errors="ignore")
                decoded = decoded.split("\x00", 1)[0].rstrip("\uffff")
                if decoded:
                    name = decoded
        pending_lfn.clear()

        clus_hi, _wt, _wd, clus_lo, file_size = struct.unpack_from("<HHHHI", ent, 20)
        first_cluster = ((clus_hi << 16) | clus_lo) if fat_bits == 32 else clus_lo
        entries.append(
            _DirEntry(
                name=name,
                short_name=short11,
                attr=attr,
                first_cluster=first_cluster,
                size=file_size,
                entry_index=idx,
            )
        )

    return entries, shorts, end_index


class _FatVolume:
    def __init__(self, io: _BlockIO, esp_offset: int, esp_size: int) -> None:
        self._io = io
        self._esp_offset = esp_offset
        self._esp_size = esp_size

        boot = io.read_at(esp_offset, _SECTOR_SIZE)
        if len(boot) < _SECTOR_SIZE or boot[510:512] != b"\x55\xAA":
            raise WriteError("Invalid EFI-SYSTEM FAT boot sector.")

        (
            bps,
            spc,
            rsvd,
            nfats,
            root_cnt,
            tot16,
            _media,
            spf16,
            _spt,
            _heads,
            _hid,
            tot32,
        ) = struct.unpack_from("<HBHBHHBHHHII", boot, 11)

        if bps < _SECTOR_SIZE or bps % _SECTOR_SIZE != 0 or spc == 0 or (spc & (spc - 1)) != 0:
            raise WriteError("Unsupported EFI-SYSTEM FAT geometry.")
        if nfats == 0 or rsvd == 0:
            raise WriteError("Invalid EFI-SYSTEM FAT layout.")

        spf32, _flags, _ver, root_clus, fsinfo_sec = struct.unpack_from("<IHHIH", boot, 36)
        self.bytes_per_sector = bps
        self.sectors_per_cluster = spc
        self.cluster_bytes = bps * spc
        self.reserved_sectors = rsvd
        self.num_fats = nfats
        self.root_entry_count = root_cnt
        self.sectors_per_fat = spf16 if spf16 != 0 else spf32
        self.total_sectors = tot16 if tot16 != 0 else tot32

        if self.sectors_per_fat == 0 or self.total_sectors * bps > esp_size:
            raise WriteError("EFI-SYSTEM FAT bounds exceed partition size.")

        self.root_dir_sectors = ((root_cnt * 32) + (bps - 1)) // bps
        self.first_data_sector = rsvd + nfats * self.sectors_per_fat + self.root_dir_sectors
        data_sectors = self.total_sectors - self.first_data_sector
        if data_sectors <= 0:
            raise WriteError("EFI-SYSTEM FAT volume has no data clusters.")

        self.cluster_count = data_sectors // spc
        if 4085 <= self.cluster_count < 65525:
            self.fat_bits = 16
            self.eoc_value = 0xFFFF
            self.root_cluster = 0
            self.fsinfo_sector = 0
        elif self.cluster_count >= 65525:
            self.fat_bits = 32
            self.eoc_value = 0x0FFFFFFF
            self.root_cluster = root_clus
            self.fsinfo_sector = fsinfo_sec
        else:
            raise WriteError("Unsupported EFI-SYSTEM FAT12 filesystem.")

        self.fat_bytes = self.sectors_per_fat * bps
        fat1_off = self._esp_offset + self.reserved_sectors * bps
        self.fat = bytearray(self._io.read_at(fat1_off, self.fat_bytes))

    def _cluster_offset(self, cluster: int) -> int:
        if cluster < 2 or cluster >= self.cluster_count + 2:
            raise WriteError(f"FAT cluster index out of range: {cluster}")
        sector = self.first_data_sector + (cluster - 2) * self.sectors_per_cluster
        return self._esp_offset + sector * self.bytes_per_sector

    def _get_fat(self, cluster: int) -> int:
        if self.fat_bits == 16:
            return int(struct.unpack_from("<H", self.fat, cluster * 2)[0])
        val = int(struct.unpack_from("<I", self.fat, cluster * 4)[0])
        return val & 0x0FFFFFFF

    def _set_fat(self, cluster: int, val: int) -> None:
        if self.fat_bits == 16:
            struct.pack_into("<H", self.fat, cluster * 2, val & 0xFFFF)
        else:
            old = int(struct.unpack_from("<I", self.fat, cluster * 4)[0])
            merged = (old & 0xF0000000) | (val & 0x0FFFFFFF)
            struct.pack_into("<I", self.fat, cluster * 4, merged)

    def _is_eof(self, val: int) -> bool:
        return val >= (0xFFF8 if self.fat_bits == 16 else 0x0FFFFFF8)

    def _chain(self, start: int) -> list[int]:
        if start < 2:
            return []
        out: list[int] = []
        seen: set[int] = set()
        curr = start
        while True:
            if curr < 2 or curr >= self.cluster_count + 2 or curr in seen:
                raise WriteError("Corrupt FAT cluster chain on EFI-SYSTEM partition.")
            seen.add(curr)
            out.append(curr)
            nxt = self._get_fat(curr)
            if self._is_eof(nxt):
                break
            curr = nxt
        return out

    def _free_chain(self, start: int) -> None:
        if start < 2:
            return
        for c in self._chain(start):
            self._set_fat(c, 0)

    def _alloc_chain(self, count: int) -> list[int]:
        if count <= 0:
            return []
        allocated: list[int] = []
        max_cluster = self.cluster_count + 2
        for c in range(2, max_cluster):
            if self._get_fat(c) == 0:
                allocated.append(c)
                if len(allocated) == count:
                    break
        if len(allocated) < count:
            raise WriteError("EFI-SYSTEM partition has insufficient free space for flexshim.")
        for i, c in enumerate(allocated):
            nxt = allocated[i + 1] if i + 1 < len(allocated) else self.eoc_value
            self._set_fat(c, nxt)
        return allocated

    def _write_fats(self) -> None:
        for i in range(self.num_fats):
            off = self._esp_offset + (self.reserved_sectors + i * self.sectors_per_fat) * self.bytes_per_sector
            self._io.write_at(off, self.fat)

        if self.fat_bits == 32 and 0 < self.fsinfo_sector < self.reserved_sectors:
            fsi_off = self._esp_offset + self.fsinfo_sector * self.bytes_per_sector
            fsi = bytearray(self._io.read_at(fsi_off, _SECTOR_SIZE))
            if len(fsi) == _SECTOR_SIZE and fsi[:4] == b"RRaA" and fsi[484:488] == b"rrAa" and fsi[508:512] == b"\x00\x00\x55\xAA":
                struct.pack_into("<II", fsi, 488, 0xFFFFFFFF, 0xFFFFFFFF)
                self._io.write_at(fsi_off, fsi)

    def _read_dir_bytes(self, clusters: Sequence[int] | None) -> bytearray:
        if clusters is None:
            off = self._esp_offset + (self.reserved_sectors + self.num_fats * self.sectors_per_fat) * self.bytes_per_sector
            return bytearray(self._io.read_at(off, self.root_dir_sectors * self.bytes_per_sector))
        buf = bytearray()
        for c in clusters:
            buf.extend(self._io.read_at(self._cluster_offset(c), self.cluster_bytes))
        return buf

    def _write_dir_bytes(self, clusters: Sequence[int] | None, buf: bytearray) -> None:
        if clusters is None:
            off = self._esp_offset + (self.reserved_sectors + self.num_fats * self.sectors_per_fat) * self.bytes_per_sector
            self._io.write_at(off, buf)
            return
        for idx, c in enumerate(clusters):
            chunk = memoryview(buf)[idx * self.cluster_bytes : (idx + 1) * self.cluster_bytes]
            self._io.write_at(self._cluster_offset(c), chunk)

    def _find_dir_clusters(self, dir_parts: Sequence[str]) -> list[int] | None:
        clusters: list[int] | None = None if self.fat_bits == 16 else self._chain(self.root_cluster)
        for part in dir_parts:
            buf = self._read_dir_bytes(clusters)
            entries, _shorts, _end = _parse_directory(buf, self.fat_bits)
            target = next((e for e in entries if (e.attr & _ATTR_DIR) and e.name.lower() == part.lower()), None)
            if target is None or target.first_cluster < 2:
                raise WriteError(f"Directory /{ '/'.join(dir_parts) } missing on EFI-SYSTEM partition.")
            clusters = self._chain(target.first_cluster)
        return clusters

    def read_file(self, parts: Sequence[str]) -> bytes | None:
        if not parts:
            return None
        try:
            clusters = self._find_dir_clusters(parts[:-1])
        except WriteError:
            return None
        buf = self._read_dir_bytes(clusters)
        entries, _shorts, _end = _parse_directory(buf, self.fat_bits)
        target_name = parts[-1].lower()
        target = next((e for e in entries if not (e.attr & _ATTR_DIR) and e.name.lower() == target_name), None)
        if target is None:
            return None
        if target.size == 0:
            return b""
        chain = self._chain(target.first_cluster)
        out = bytearray()
        for c in chain:
            out.extend(self._io.read_at(self._cluster_offset(c), self.cluster_bytes))
        return bytes(out[: target.size])

    def _write_data_clusters(self, data: bytes) -> int:
        if not data:
            return 0
        n_clusters = (len(data) + self.cluster_bytes - 1) // self.cluster_bytes
        chain = self._alloc_chain(n_clusters)
        for idx, c in enumerate(chain):
            chunk = data[idx * self.cluster_bytes : (idx + 1) * self.cluster_bytes]
            if len(chunk) < self.cluster_bytes:
                padded = bytearray(self.cluster_bytes)
                padded[: len(chunk)] = chunk
                self._io.write_at(self._cluster_offset(c), padded)
            else:
                self._io.write_at(self._cluster_offset(c), chunk)
        return chain[0]

    def write_file(self, dir_parts: Sequence[str], filename: str, data: bytes) -> None:
        clusters = self._find_dir_clusters(dir_parts)
        buf = self._read_dir_bytes(clusters)
        entries, shorts, end_index = _parse_directory(buf, self.fat_bits)
        existing = next(
            (e for e in entries if not (e.attr & _ATTR_DIR) and e.name.lower() == filename.lower()),
            None,
        )

        if existing is not None:
            self._free_chain(existing.first_cluster)
            new_clus = self._write_data_clusters(data)
            off = existing.entry_index * 32
            clus_hi = (new_clus >> 16) & 0xFFFF if self.fat_bits == 32 else 0
            clus_lo = new_clus & 0xFFFF
            struct.pack_into(
                "<HHHHI",
                buf,
                off + 20,
                clus_hi,
                _DOS_DEFAULT_TIME,
                _DOS_DEFAULT_DATE,
                clus_lo,
                len(data),
            )
            self._write_dir_bytes(clusters, buf)
            self._write_fats()
            return

        new_clus = self._write_data_clusters(data)
        short11 = _make_short_name(filename, shorts)
        lfn_records = _encode_lfn_entries(filename, _lfn_checksum(short11)) if _needs_lfn(filename) else []
        clus_hi = (new_clus >> 16) & 0xFFFF if self.fat_bits == 32 else 0
        clus_lo = new_clus & 0xFFFF
        short_rec = struct.pack(
            "<11sBBBHHHHHHHI",
            short11,
            _ATTR_ARCHIVE,
            0,
            0,
            _DOS_DEFAULT_TIME,
            _DOS_DEFAULT_DATE,
            _DOS_DEFAULT_DATE,
            clus_hi,
            _DOS_DEFAULT_TIME,
            _DOS_DEFAULT_DATE,
            clus_lo,
            len(data),
        )
        new_records = [*lfn_records, short_rec]
        needed = len(new_records)
        total_slots = len(buf) // 32

        insert_at = end_index
        if insert_at + needed > total_slots:
            run = 0
            reusable_start: int | None = None
            for idx in range(end_index):
                if buf[idx * 32] == 0xE5:
                    if run == 0:
                        reusable_start = idx
                    run += 1
                    if run == needed:
                        break
                else:
                    run = 0
                    reusable_start = None
            if run == needed and reusable_start is not None:
                insert_at = reusable_start
            elif clusters is None:
                raise WriteError("EFI-SYSTEM root directory has no free entries.")
            else:
                extra_clusters_needed = ((end_index + needed) * 32 - len(buf) + self.cluster_bytes - 1) // self.cluster_bytes
                extra_chain = self._alloc_chain(extra_clusters_needed)
                self._set_fat(clusters[-1], extra_chain[0])
                clusters.extend(extra_chain)
                buf.extend(b"\x00" * (extra_clusters_needed * self.cluster_bytes))

        for pos, rec in enumerate(new_records):
            off = (insert_at + pos) * 32
            buf[off : off + 32] = rec

        self._write_dir_bytes(clusters, buf)
        self._write_fats()


def install_flexshim_to_esp(
    io: _BlockIO,
    esp_offset: int,
    esp_size: int,
    *,
    cros_debug: bool = False,
    efi_bytes: bytes | None = None,
    cfg_bytes: bytes | None = None,
) -> tuple[bytes, bytes]:
    efi = efi_bytes if efi_bytes is not None else load_flexshim_efi()
    cfg = cfg_bytes if cfg_bytes is not None else render_flexshim_cfg(cros_debug=cros_debug)

    vol = _FatVolume(io, esp_offset, esp_size)
    if vol.read_file(("efi", "boot", "crdybootx64.efi")) is None:
        raise WriteError("ChromeOS Flex crdybootx64.efi not found on EFI-SYSTEM partition.")

    vol.write_file(("efi", "boot"), "bootx64.efi", efi)
    vol.write_file(("efi", "boot"), "flexshim.cfg", cfg)
    return efi, cfg


def verify_flexshim_on_esp(
    io: _BlockIO,
    esp_offset: int,
    esp_size: int,
    expected_efi: bytes,
    expected_cfg: bytes,
) -> None:
    vol = _FatVolume(io, esp_offset, esp_size)
    if vol.read_file(("efi", "boot", "crdybootx64.efi")) is None:
        raise WriteError("Verification failed: crdybootx64.efi missing from EFI-SYSTEM partition.")
    actual_efi = vol.read_file(("efi", "boot", "bootx64.efi"))
    if actual_efi != expected_efi:
        raise WriteError("Verification failed: flexshimx64.efi read back from EFI-SYSTEM did not match.")
    actual_cfg = vol.read_file(("efi", "boot", "flexshim.cfg"))
    if actual_cfg != expected_cfg:
        raise WriteError("Verification failed: flexshim.cfg read back from EFI-SYSTEM did not match.")
