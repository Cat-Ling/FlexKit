#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEX_ZIP="${FLEX_ZIP:-/mnt/d/acer_workbench/chromeOS/chromeos_16700.68.0_reven_recovery_ltc-channel_RevenMPKeys-v11.bin.zip}"
OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}"
FLEXSHIM_EFI="$ROOT/build/flexshimx64.efi"
FAULT_INJ_MANIFEST="$ROOT/tests/fault_injector/Cargo.toml"
FAULT_INJ_EFI="$ROOT/tests/fault_injector/target/x86_64-unknown-uefi/release/fault_injector.efi"

# LBA 299008..823295 holds EFI-SYSTEM; add 128 sectors for backup GPT relocation.
ESP_START_LBA=299008
ESP_BYTE_OFFSET=$((ESP_START_LBA * 512))
SLICE_BYTES=$(((823296 + 128) * 512))

for cmd in qemu-system-x86_64 sgdisk mcopy mmd mdir mkfs.vfat python3 cargo; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "missing required tool: $cmd" >&2
        exit 1
    fi
done

if [ ! -r "$FLEX_ZIP" ]; then
    echo "missing ChromeOS Flex archive: $FLEX_ZIP" >&2
    exit 1
fi
if [ ! -r "$OVMF_CODE" ] || [ ! -r "$OVMF_VARS" ]; then
    echo "missing OVMF firmware files" >&2
    exit 1
fi
if [ ! -r "$FLEXSHIM_EFI" ]; then
    echo "missing built binary: $FLEXSHIM_EFI" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR"
}
trap cleanup EXIT

cargo build --release --manifest-path "$FAULT_INJ_MANIFEST" --target x86_64-unknown-uefi

python3 - "$FLEX_ZIP" "$TMPDIR/flex_slice.img" "$SLICE_BYTES" <<'PY'
import struct, sys, zipfile, zlib

zip_path, out_path, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
with zipfile.ZipFile(zip_path) as zf:
    entry = zf.namelist()[0]
    remaining = limit
    with zf.open(entry) as src, open(out_path, "wb") as dst:
        while remaining > 0:
            chunk = src.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)

# Patch GPT on the truncated slice: remove out-of-range p1/p3/p5 and relocate backup GPT.
total_sectors = limit // 512
with open(out_path, "r+b") as f:
    f.seek(512)
    pri_hdr = bytearray(f.read(512))
    f.seek(1024)
    part_tbl = bytearray(f.read(128 * 128))

    for part_idx in (0, 2, 4):
        off = part_idx * 128
        part_tbl[off:off + 128] = b"\x00" * 128

    part_crc = zlib.crc32(part_tbl) & 0xFFFFFFFF
    backup_lba = total_sectors - 1
    last_usable_lba = total_sectors - 34

    struct.pack_into("<QQ", pri_hdr, 24, 1, backup_lba)
    struct.pack_into("<Q", pri_hdr, 48, last_usable_lba)
    struct.pack_into("<Q", pri_hdr, 72, 2)
    struct.pack_into("<I", pri_hdr, 88, part_crc)
    struct.pack_into("<I", pri_hdr, 16, 0)
    pri_crc = zlib.crc32(pri_hdr[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", pri_hdr, 16, pri_crc)

    bak_hdr = bytearray(pri_hdr)
    bak_part_lba = total_sectors - 33
    struct.pack_into("<QQ", bak_hdr, 24, backup_lba, 1)
    struct.pack_into("<Q", bak_hdr, 72, bak_part_lba)
    struct.pack_into("<I", bak_hdr, 16, 0)
    bak_crc = zlib.crc32(bak_hdr[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", bak_hdr, 16, bak_crc)

    f.seek(512)
    f.write(pri_hdr)
    f.seek(1024)
    f.write(part_tbl)
    f.seek(bak_part_lba * 512)
    f.write(part_tbl)
    f.seek(backup_lba * 512)
    f.write(bak_hdr)
PY

sgdisk -v "$TMPDIR/flex_slice.img" >/dev/null

: > "$TMPDIR/empty"
mcopy -o -i "$TMPDIR/flex_slice.img@@$ESP_BYTE_OFFSET" "$TMPDIR/empty" "::/efi/boot/crdyboot_verbose"
mcopy -i "$TMPDIR/flex_slice.img@@$ESP_BYTE_OFFSET" "::/efi/boot/crdybootx64.efi" "$TMPDIR/crdybootx64.efi"

KVM_FLAG=(-cpu max)
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    KVM_FLAG=(-enable-kvm -cpu host)
fi

run_qemu_until() {
    local log_file="$1"
    local stop_regex="$2"
    shift 2

    : > "$log_file"
    qemu-system-x86_64 \
        "${KVM_FLAG[@]}" \
        -machine q35 \
        -m 2048 \
        -display none \
        -serial "file:$log_file" \
        -monitor none \
        -no-reboot \
        "$@" >/dev/null 2>&1 &
    local pid=$!

    local elapsed=0
    local max_ticks=150
    while [ "$elapsed" -lt "$max_ticks" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        if grep -qE "$stop_regex" "$log_file" 2>/dev/null; then
            sleep 0.5
            break
        fi
        sleep 0.2
        elapsed=$((elapsed + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
}

echo "=== Case 1: Reproduce upstream crdyboot failure without flexshim ==="
cp "$TMPDIR/flex_slice.img" "$TMPDIR/case1.img"
cp "$OVMF_VARS" "$TMPDIR/vars1.fd"
mcopy -o -i "$TMPDIR/case1.img@@$ESP_BYTE_OFFSET" "$FAULT_INJ_EFI" "::/efi/boot/bootx64.efi"
mcopy -o -i "$TMPDIR/case1.img@@$ESP_BYTE_OFFSET" "$TMPDIR/crdybootx64.efi" "::/efi/boot/nextx64.efi"

run_qemu_until "$TMPDIR/case1.log" "RefCell already borrowed|BorrowMutError" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$TMPDIR/vars1.fd" \
    -drive "id=d0,if=none,format=raw,file=$TMPDIR/case1.img" \
    -device "virtio-blk-pci,drive=d0,bootindex=0"

if ! grep -q "failed to write revocation variable: INVALID_PARAMETER" "$TMPDIR/case1.log"; then
    echo "Case 1 FAIL: missing revocation error" >&2
    cat "$TMPDIR/case1.log" >&2
    exit 1
fi
if ! grep -q "panicked at libcrdy/src/logging.rs:104:22" "$TMPDIR/case1.log" \
   || ! grep -qE "RefCell already borrowed|already borrowed: BorrowMutError" "$TMPDIR/case1.log"; then
    echo "Case 1 FAIL: missing logging.rs BorrowMutError panic" >&2
    cat "$TMPDIR/case1.log" >&2
    exit 1
fi
echo "Case 1 PASS"

echo "=== Case 2: Same-disk flexshim fixes variable errors and restores gRT on ExitBootServices ==="
cp "$TMPDIR/flex_slice.img" "$TMPDIR/case2.img"
cp "$OVMF_VARS" "$TMPDIR/vars2.fd"
mcopy -o -i "$TMPDIR/case2.img@@$ESP_BYTE_OFFSET" "$FAULT_INJ_EFI" "::/efi/boot/bootx64.efi"
mcopy -o -i "$TMPDIR/case2.img@@$ESP_BYTE_OFFSET" "$FLEXSHIM_EFI" "::/efi/boot/nextx64.efi"

run_qemu_until "$TMPDIR/case2.log" "FAULT_INJ: RT_RESTORE_" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$TMPDIR/vars2.fd" \
    -drive "id=d0,if=none,format=raw,file=$TMPDIR/case2.img" \
    -device "virtio-blk-pci,drive=d0,bootindex=0"

for required in \
    "flexshim: installing UEFI variable compatibility shim" \
    "vboot cmdline:" \
    "entry_point:" \
    "FAULT_INJ: RT_RESTORE_OK"; do
    if ! grep -q "$required" "$TMPDIR/case2.log"; then
        echo "Case 2 FAIL: missing '$required'" >&2
        cat "$TMPDIR/case2.log" >&2
        exit 1
    fi
done
if grep -qE "failed to write revocation variable|RefCell already borrowed|BorrowMutError|\[PANIC\]" "$TMPDIR/case2.log"; then
    echo "Case 2 FAIL: unexpected error/panic in log" >&2
    cat "$TMPDIR/case2.log" >&2
    exit 1
fi
echo "Case 2 PASS"

echo "=== Case 3: Cross-disk ESP discovery from separate boot disk ==="
truncate -s 36M "$TMPDIR/esp_part.img"
mkfs.vfat -F 32 "$TMPDIR/esp_part.img" >/dev/null
mmd -i "$TMPDIR/esp_part.img" ::/efi ::/efi/boot
mcopy -i "$TMPDIR/esp_part.img" "$FLEXSHIM_EFI" "::/efi/boot/bootx64.efi"

truncate -s 38M "$TMPDIR/boot_only.img"
sgdisk -n 1:2048:+36M -t 1:EF00 "$TMPDIR/boot_only.img" >/dev/null
dd if="$TMPDIR/esp_part.img" of="$TMPDIR/boot_only.img" bs=512 seek=2048 conv=notrunc status=none

cp "$TMPDIR/flex_slice.img" "$TMPDIR/case3_flex.img"
cp "$OVMF_VARS" "$TMPDIR/vars3.fd"

run_qemu_until "$TMPDIR/case3.log" "entry_point:" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$TMPDIR/vars3.fd" \
    -drive "id=d0,if=none,format=raw,file=$TMPDIR/boot_only.img" \
    -device "virtio-blk-pci,drive=d0,bootindex=0" \
    -drive "id=d1,if=none,format=raw,file=$TMPDIR/case3_flex.img" \
    -device "virtio-blk-pci,drive=d1"

for required in \
    "flexshim: installing UEFI variable compatibility shim" \
    "vboot cmdline:" \
    "entry_point:"; do
    if ! grep -q "$required" "$TMPDIR/case3.log"; then
        echo "Case 3 FAIL: missing '$required'" >&2
        cat "$TMPDIR/case3.log" >&2
        exit 1
    fi
done
if grep -qE "failed to write revocation variable|RefCell already borrowed|BorrowMutError|\[PANIC\]" "$TMPDIR/case3.log"; then
    echo "Case 3 FAIL: unexpected error/panic in log" >&2
    cat "$TMPDIR/case3.log" >&2
    exit 1
fi
echo "Case 3 PASS"

echo "=== Case 4: Active flexshim.cfg injects cros_debug and extra_cmdline into Linux EFI stub ==="
cp "$TMPDIR/flex_slice.img" "$TMPDIR/case4.img"
cp "$OVMF_VARS" "$TMPDIR/vars4.fd"
mcopy -o -i "$TMPDIR/case4.img@@$ESP_BYTE_OFFSET" "$FAULT_INJ_EFI" "::/efi/boot/bootx64.efi"
mcopy -o -i "$TMPDIR/case4.img@@$ESP_BYTE_OFFSET" "$FLEXSHIM_EFI" "::/efi/boot/nextx64.efi"
cat > "$TMPDIR/active_flexshim.cfg" <<'CFG'
block_updates=true
extra_cmdline=i8042.nomux i915.enable_psr=0
CFG
mcopy -o -i "$TMPDIR/case4.img@@$ESP_BYTE_OFFSET" "$TMPDIR/active_flexshim.cfg" "::/efi/boot/flexshim.cfg"

run_qemu_until "$TMPDIR/case4.log" "FAULT_INJ: RT_RESTORE_" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$TMPDIR/vars4.fd" \
    -drive "id=d0,if=none,format=raw,file=$TMPDIR/case4.img" \
    -device "virtio-blk-pci,drive=d0,bootindex=0"

for required in \
    "flexshim: config block_updates=true" \
    "flexshim: config extra_cmdline enabled" \
    "flexshim: applied boot options from flexshim.cfg" \
    "FAULT_INJ: KERNEL_LOAD_OPTIONS=" \
    "FAULT_INJ: RT_RESTORE_OK"; do
    if ! grep -q "$required" "$TMPDIR/case4.log"; then
        echo "Case 4 FAIL: missing '$required'" >&2
        cat "$TMPDIR/case4.log" >&2
        exit 1
    fi
done
case4_opts="$(grep "FAULT_INJ: KERNEL_LOAD_OPTIONS=" "$TMPDIR/case4.log" | head -n 1)"
for flag in "cros_efi" "i8042.nomux i915.enable_psr=0" "cros_debug"; do
    if ! printf '%s\n' "$case4_opts" | grep -q "$flag"; then
        echo "Case 4 FAIL: KERNEL_LOAD_OPTIONS missing '$flag'" >&2
        cat "$TMPDIR/case4.log" >&2
        exit 1
    fi
done
if grep -qE "failed to write revocation variable|RefCell already borrowed|BorrowMutError|\[PANIC\]" "$TMPDIR/case4.log"; then
    echo "Case 4 FAIL: unexpected error/panic in log" >&2
    cat "$TMPDIR/case4.log" >&2
    exit 1
fi
echo "Case 4 PASS"

echo "=== Case 5: Default shipped flexshim.cfg (all commented) leaves cmdline untouched ==="
cp "$TMPDIR/flex_slice.img" "$TMPDIR/case5.img"
cp "$OVMF_VARS" "$TMPDIR/vars5.fd"
mcopy -o -i "$TMPDIR/case5.img@@$ESP_BYTE_OFFSET" "$FAULT_INJ_EFI" "::/efi/boot/bootx64.efi"
mcopy -o -i "$TMPDIR/case5.img@@$ESP_BYTE_OFFSET" "$FLEXSHIM_EFI" "::/efi/boot/nextx64.efi"
mcopy -o -i "$TMPDIR/case5.img@@$ESP_BYTE_OFFSET" "$ROOT/flexshim.cfg" "::/efi/boot/flexshim.cfg"

run_qemu_until "$TMPDIR/case5.log" "FAULT_INJ: RT_RESTORE_" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$TMPDIR/vars5.fd" \
    -drive "id=d0,if=none,format=raw,file=$TMPDIR/case5.img" \
    -device "virtio-blk-pci,drive=d0,bootindex=0"

for required in \
    "flexshim: installing UEFI variable compatibility shim" \
    "entry_point:" \
    "FAULT_INJ: KERNEL_LOAD_OPTIONS=" \
    "FAULT_INJ: RT_RESTORE_OK"; do
    if ! grep -q "$required" "$TMPDIR/case5.log"; then
        echo "Case 5 FAIL: missing '$required'" >&2
        cat "$TMPDIR/case5.log" >&2
        exit 1
    fi
done
case5_opts="$(grep "FAULT_INJ: KERNEL_LOAD_OPTIONS=" "$TMPDIR/case5.log" | head -n 1)"
if ! printf '%s\n' "$case5_opts" | grep -q "cros_efi"; then
    echo "Case 5 FAIL: KERNEL_LOAD_OPTIONS missing 'cros_efi'" >&2
    cat "$TMPDIR/case5.log" >&2
    exit 1
fi
if printf '%s\n' "$case5_opts" | grep -q "cros_debug"; then
    echo "Case 5 FAIL: KERNEL_LOAD_OPTIONS unexpectedly contains 'cros_debug'" >&2
    cat "$TMPDIR/case5.log" >&2
    exit 1
fi
if grep -qE "applied boot options|ignored invalid line|failed to write revocation variable|RefCell already borrowed|BorrowMutError|\[PANIC\]" "$TMPDIR/case5.log"; then
    echo "Case 5 FAIL: unexpected log output or error" >&2
    cat "$TMPDIR/case5.log" >&2
    exit 1
fi
echo "Case 5 PASS"

echo "ALL QEMU VERIFICATION CASES PASSED"
