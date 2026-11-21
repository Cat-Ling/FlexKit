# Changelog

## [1.0.0] - 2026-11-21

- Genuine ChromeOS Flex To Go mode matching `write_base_table` (4 GiB `ROOT-B`/`ROOT-A`, expanded pure-Python `H-STATE` ext4 partition, preserved partition GUIDs, unmodified signed OS partitions, and in-place To Go OS updates preserving user data).
- Removed all ChromeOS binary/bootloader patching to preserve Verified Boot integrity.
- Bundled `flexshim` UEFI compatibility shim (`flexshimx64.efi` + `flexshim.cfg`) with Advanced Boot Options for fixing UEFI boot panics on older PCs and disabling automatic updates via config.
- Switched FlexKit license to GPL-3.0-or-later.
- Fixed Windows and macOS (`arm64` & `x86_64`) builds, tests, and GitHub Actions release pipeline; dropped FreeBSD target.

## [0.0.1] - 2026-11-08

- Initial release of FlexKit (`v0.0.1`).
- Official ChromeOS Flex catalog validation (`reven` board) across Stable, LTS, LTC, Beta, and Dev channels.
- Resumable downloader supporting `aria2c` JSON-RPC and built-in HTTP ranged transfers with fsync-backed control files.
- Streaming ZIP decompression with SHA-1 and MD5 verification against Google recovery manifests.
- Cross-platform USB drive detection (Linux, Windows, macOS, FreeBSD) with Rufus-style USB hard drive toggle and system/source-drive protection.
- Privileged block writer helper with read-back SHA-1 verification and multi-tier elevation (`sudo -n`, `pkexec`, `doas`, `osascript`, UAC, and built-in Material 3 password dialog).
- Responsive Material 3 Light wizard interface with bundled Google Sans Flex typography.
