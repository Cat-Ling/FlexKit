// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

use crate::uefi::TableHeader;
use core::mem::size_of;
use core::slice;

const CRC32_POLYNOMIAL: u32 = 0xEDB8_8320;

/// Computes the standard IEEE 802.3 CRC32 checksum used in UEFI table headers.
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = !0u32;
    for &b in bytes {
        crc ^= u32::from(b);
        let mut bit = 0;
        while bit < 8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (CRC32_POLYNOMIAL & mask);
            bit += 1;
        }
    }
    !crc
}

/// Recomputes `hdr.crc32` over `hdr.header_size` bytes with `hdr.crc32` zeroed.
///
/// Uses `hdr.header_size` rather than a compiled struct size because older UEFI
/// firmware tables can be shorter than newer revisions of the specification.
///
/// # Safety
/// `hdr` must point to a valid, mutable UEFI table header whose backing memory
/// spans at least `hdr.header_size` readable bytes.
pub unsafe fn update_table_crc32(hdr: *mut TableHeader) {
    if hdr.is_null() {
        return;
    }
    let header_size = unsafe { (*hdr).header_size as usize };
    if header_size < size_of::<TableHeader>() {
        return;
    }
    unsafe {
        (*hdr).crc32 = 0;
        let raw = slice::from_raw_parts(hdr.cast::<u8>(), header_size);
        (*hdr).crc32 = crc32(raw);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[repr(C)]
    struct MockTable {
        hdr: TableHeader,
        payload: [u64; 3],
    }

    #[test]
    fn crc32_known_vectors() {
        assert_eq!(crc32(b""), 0x0000_0000);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn update_table_crc32_zeroes_field_and_uses_header_size() {
        let mut table = MockTable {
            hdr: TableHeader {
                signature: 0x5452_554E_5453_5953,
                revision: 0x0002_0000,
                header_size: size_of::<MockTable>() as u32,
                crc32: 0xDEAD_BEEF,
                reserved: 0,
            },
            payload: [0x1122_3344_5566_7788, 0xAABB_CCDD_EEFF_0011, 42],
        };

        unsafe {
            update_table_crc32(&raw mut table.hdr);
        }
        let recorded = table.hdr.crc32;
        assert_ne!(recorded, 0);
        assert_ne!(recorded, 0xDEAD_BEEF);

        table.hdr.crc32 = 0;
        let raw = unsafe {
            slice::from_raw_parts((&raw const table).cast::<u8>(), size_of::<MockTable>())
        };
        assert_eq!(recorded, crc32(raw));
    }
}
