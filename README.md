# FlexKit

Cross-platform **ChromeOS Flex** downloader and bootable USB installer for Linux, Windows, macOS, and FreeBSD.

## Features

- **Official ChromeOS Flex catalog**: Pulls `cloudready_recovery2.json` directly from `dl.google.com` and enforces strict `reven` board validation across Stable, LTS, LTC, Beta, and Dev channels.
- **Crash-safe resumable downloads**: Uses `aria2c` JSON-RPC when available on `PATH`, or falls back to a built-in ranged HTTP downloader with fsync-committed `.part.json` control files and `If-Range` ETag validation.
- **Zero-bloat verification & flashing**: Verifies the downloaded `.zip` against Google's SHA-1 and MD5 digests via streaming decompression without writing a separate 9.5 GB `.bin` to disk, then streams directly to the target USB block device with read-back SHA-1 verification.
- **Rufus-style drive safety**: Hides system/boot/read-only disks, disables drives smaller than the image or holding the downloaded archive, and provides a **Show USB hard drives** toggle for external USB SSDs/HDDs.
- **Least-privilege architecture**: The GUI always runs unprivileged. Only a headless `--write` helper is elevated (`sudo -n`, `doas -n`, `pkexec`, `osascript`, Windows UAC, or a built-in Material 3 password sheet when no GUI sudo agent is available).
- **Consistent Material 3 Light UI**: Bundled open-source Google Sans Flex font and custom-painted Material 3 controls render identically on every OS and adapt responsively down to `360×520`.

## Running from source

```sh
PYTHONPATH=src python3 -m flexkit
```

## Versioning

```sh
python3 scripts/version.py show
python3 scripts/version.py bump patch
```

## Building native binaries

Linux / macOS / FreeBSD:
```sh
./scripts/build.sh
```

Windows (PowerShell):
```powershell
.\scripts\build.ps1
```

Artifacts and `.sha256` digests are written to `dist/`.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
