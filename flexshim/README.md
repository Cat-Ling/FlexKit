# flexshim

UEFI variable compatibility shim that chainloads `\efi\boot\crdybootx64.efi` on
early UEFI 2.0 systems (such as Sandy Bridge laptops with Phoenix UEFI
firmware) where `SetVariable` failures cause ChromeOS Flex `crdyboot` revocation
errors and a re-entrant `BorrowMutError` panic in `logging.rs`.

## How It Works

1. Hooks `RuntimeServices->GetVariable` and `RuntimeServices->SetVariable` and
   updates `RuntimeServices->Hdr.CRC32`.
2. Services `crdyboot`, `SBAT`, and `fwupd` bootloader variables from an
   in-memory shadow table (and treats `CrdybootLog`/`CrdyshimLog` as success
   sinks) while forwarding all other variable calls to firmware.
3. Registers an `EVT_SIGNAL_EXIT_BOOT_SERVICES` callback at `TPL_NOTIFY` to
   restore the original `RuntimeServices` pointers and CRC32 before the Linux
   kernel takes ownership of runtime services.
4. Searches the shim's own EFI System Partition first, then all other
   `SimpleFileSystem` handles, for `\efi\boot\crdybootx64.efi` and chainloads
   it via `LoadImage` and `StartImage`.
5. Optionally reads `flexshim.cfg` from the shim's directory and appends
   configured kernel options (`cros_debug` or `extra_cmdline`) to `crdyboot`'s
   `LoadedImage->LoadOptions` after `vboot` verification completes.

## Requirements

- Rust toolchain (`rustc` and `cargo`) with the `x86_64-unknown-uefi` target
  (`rustup target add x86_64-unknown-uefi`)
- For QEMU verification: `qemu-system-x86_64`, `ovmf` (`/usr/share/OVMF/OVMF_CODE_4M.fd`),
  `sgdisk`, `mtools`, `mkfs.vfat`, and `python3`

## Building and Testing

```sh
make test
make all
```

The built UEFI binary and default commented configuration file are written to
`build/flexshimx64.efi` and `build/flexshim.cfg`.

To run the end-to-end QEMU/OVMF test suite against a ChromeOS Flex recovery
image (read-only):

```sh
make verify-qemu
```

## Installation

`flexshimx64.efi` is unsigned and requires UEFI Secure Boot to be disabled.

### ChromeOS Flex USB Installer (Direct Replacement)

Mount partition 12 (`EFI-SYSTEM`) of the flashed ChromeOS Flex USB drive and
replace `\efi\boot\bootx64.efi` with `build/flexshimx64.efi`, keeping
`\efi\boot\crdybootx64.efi` in place. On a live USB or installer image, create
a new boot entry or swap `\efi\boot\bootx64.efi` on the ESP.

### Persistent Internal Disk Boot Entry

To survive ChromeOS Flex A/B updates without overwriting `flexshimx64.efi`,
place it in a separate directory on an EFI System Partition (for example
`\EFI\flexshim\flexshimx64.efi` on an existing ESP or the Flex ESP) and either:

- Register a UEFI boot entry using `efibootmgr`:

```sh
efibootmgr --create --disk /dev/sda --part 1 --label "ChromeOS Flex (flexshim)" --loader '\EFI\flexshim\flexshimx64.efi'
```

- Or chainload it from GRUB:

```text
menuentry "ChromeOS Flex" {
    search --no-floppy --file --set=root /EFI/flexshim/flexshimx64.efi
    chainloader /EFI/flexshim/flexshimx64.efi
}
```

When booted from a separate partition, `flexshimx64.efi` scans attached
filesystems and automatically chainloads `\efi\boot\crdybootx64.efi` from the
ChromeOS Flex ESP.

## Configuration (`flexshim.cfg`)

Place `flexshim.cfg` in the same directory as `flexshimx64.efi`. If the file is
absent or all options are commented out, `flexshimx64.efi` leaves the `vboot`
kernel command line unmodified.

- `block_updates=true`: Appends `cros_debug` to the kernel command line.
  `update_engine` treats the booted image as a non-official debug build and
  blocks periodic background updates without wiping stateful user data. Side
  effects: enables `crosh` `shell` / VT2, leaves manual update checks in Settings
  enabled (unless `CHROMEOS_AUSERVER` is overridden in stateful `lsb-release`),
  and may restrict strict DRM playback such as HD Widevine.
- `extra_cmdline=...`: Space-separated Linux kernel parameters appended to the
  `vboot` command line (for example `i8042.nomux i915.enable_psr=0`).

## License

GNU General Public License v3.0 or later. See [COPYING](COPYING).
