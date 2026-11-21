// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

use crate::uefi::{CRDYBOOT_VAR_VENDOR, FWUPDATE_VENDOR, Guid, SBAT_VAR_VENDOR, Status};

pub const MAX_ENTRIES: usize = 16;
pub const MAX_NAME_LEN: usize = 64;
pub const MAX_DATA_LEN: usize = 1024;

const CRDYBOOT_LOG_NAME: [u16; 11] = [
    b'C' as u16,
    b'r' as u16,
    b'd' as u16,
    b'y' as u16,
    b'b' as u16,
    b'o' as u16,
    b'o' as u16,
    b't' as u16,
    b'L' as u16,
    b'o' as u16,
    b'g' as u16,
];

const CRDYSHIM_LOG_NAME: [u16; 11] = [
    b'C' as u16,
    b'r' as u16,
    b'd' as u16,
    b'y' as u16,
    b's' as u16,
    b'h' as u16,
    b'i' as u16,
    b'm' as u16,
    b'L' as u16,
    b'o' as u16,
    b'g' as u16,
];

pub fn is_intercepted_vendor(vendor: &Guid) -> bool {
    *vendor == CRDYBOOT_VAR_VENDOR || *vendor == SBAT_VAR_VENDOR || *vendor == FWUPDATE_VENDOR
}

pub fn is_shadow_only_vendor(vendor: &Guid) -> bool {
    *vendor == CRDYBOOT_VAR_VENDOR || *vendor == SBAT_VAR_VENDOR
}

pub fn is_crdyboot_log_var(name: &[u16], vendor: &Guid) -> bool {
    *vendor == CRDYBOOT_VAR_VENDOR && name == CRDYBOOT_LOG_NAME
}

pub fn is_sink_only_var(name: &[u16], vendor: &Guid) -> bool {
    is_crdyboot_log_var(name, vendor)
        || (*vendor == CRDYBOOT_VAR_VENDOR && name == CRDYSHIM_LOG_NAME)
}

#[derive(Copy, Clone)]
struct ShadowEntry {
    in_use: bool,
    vendor: Guid,
    attributes: u32,
    name_len: usize,
    name: [u16; MAX_NAME_LEN],
    data_len: usize,
    data: [u8; MAX_DATA_LEN],
}

impl ShadowEntry {
    const fn empty() -> Self {
        Self {
            in_use: false,
            vendor: Guid::new(0, 0, 0, [0; 8]),
            attributes: 0,
            name_len: 0,
            name: [0; MAX_NAME_LEN],
            data_len: 0,
            data: [0; MAX_DATA_LEN],
        }
    }

    fn matches(&self, name: &[u16], vendor: &Guid) -> bool {
        self.in_use && self.vendor == *vendor && self.name_len == name.len() && {
            self.name[..self.name_len] == *name
        }
    }
}

/// Fixed-capacity in-memory store for UEFI variables that fail on early firmware.
pub struct ShadowStore {
    entries: [ShadowEntry; MAX_ENTRIES],
}

impl ShadowStore {
    pub const fn new() -> Self {
        Self {
            entries: [ShadowEntry::empty(); MAX_ENTRIES],
        }
    }

    pub fn set(&mut self, name: &[u16], vendor: &Guid, attributes: u32, data: &[u8]) -> Status {
        if name.is_empty() {
            return Status::INVALID_PARAMETER;
        }
        // Bootloader log variables are write-only and can exceed static entry limits.
        if is_sink_only_var(name, vendor) {
            return Status::SUCCESS;
        }

        if data.is_empty() {
            for entry in &mut self.entries {
                if entry.matches(name, vendor) {
                    entry.in_use = false;
                    entry.name_len = 0;
                    entry.data_len = 0;
                    return Status::SUCCESS;
                }
            }
            return Status::NOT_FOUND;
        }

        if name.len() > MAX_NAME_LEN || data.len() > MAX_DATA_LEN {
            return Status::OUT_OF_RESOURCES;
        }

        let mut free_idx: Option<usize> = None;
        for (idx, entry) in self.entries.iter_mut().enumerate() {
            if entry.matches(name, vendor) {
                entry.attributes = attributes;
                entry.data_len = data.len();
                entry.data[..data.len()].copy_from_slice(data);
                return Status::SUCCESS;
            }
            if !entry.in_use && free_idx.is_none() {
                free_idx = Some(idx);
            }
        }

        let Some(idx) = free_idx else {
            return Status::OUT_OF_RESOURCES;
        };

        let entry = &mut self.entries[idx];
        entry.in_use = true;
        entry.vendor = *vendor;
        entry.attributes = attributes;
        entry.name_len = name.len();
        entry.name[..name.len()].copy_from_slice(name);
        entry.data_len = data.len();
        entry.data[..data.len()].copy_from_slice(data);
        Status::SUCCESS
    }

    pub fn get(
        &self,
        name: &[u16],
        vendor: &Guid,
        out_attrs: Option<&mut u32>,
        out_buf: Option<&mut [u8]>,
        out_size: &mut usize,
    ) -> Status {
        if name.is_empty() {
            return Status::INVALID_PARAMETER;
        }

        let Some(entry) = self.entries.iter().find(|e| e.matches(name, vendor)) else {
            return Status::NOT_FOUND;
        };

        if let Some(attrs) = out_attrs {
            *attrs = entry.attributes;
        }

        let required = entry.data_len;
        let caller_cap = match &out_buf {
            Some(buf) => (*out_size).min(buf.len()),
            None => 0,
        };
        *out_size = required;
        if required > caller_cap {
            return Status::BUFFER_TOO_SMALL;
        }

        if let Some(buf) = out_buf {
            buf[..required].copy_from_slice(&entry.data[..required]);
        }
        Status::SUCCESS
    }
}

impl Default for ShadowStore {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn utf16(s: &str) -> [u16; 32] {
        let mut out = [0u16; 32];
        for (i, ch) in s.encode_utf16().enumerate() {
            out[i] = ch;
        }
        out
    }

    fn utf16_slice<'a>(buf: &'a [u16; 32], s: &str) -> &'a [u16] {
        &buf[..s.encode_utf16().count()]
    }

    #[test]
    fn set_get_overwrite_and_delete_round_trip() {
        let mut store = ShadowStore::new();
        let name_buf = utf16("crdyboot_min_lvl");
        let name = utf16_slice(&name_buf, "crdyboot_min_lvl");
        let attrs = 0x03;

        let mut size = 4usize;
        let mut out = [0u8; 8];
        assert_eq!(
            store.get(name, &CRDYBOOT_VAR_VENDOR, None, Some(&mut out), &mut size),
            Status::NOT_FOUND
        );

        assert_eq!(
            store.set(name, &CRDYBOOT_VAR_VENDOR, attrs, &[1, 0, 0, 0]),
            Status::SUCCESS
        );

        let mut got_attrs = 0u32;
        size = 2;
        assert_eq!(
            store.get(
                name,
                &CRDYBOOT_VAR_VENDOR,
                Some(&mut got_attrs),
                Some(&mut out[..2]),
                &mut size
            ),
            Status::BUFFER_TOO_SMALL
        );
        assert_eq!(size, 4);
        assert_eq!(got_attrs, attrs);

        size = 0;
        assert_eq!(
            store.get(name, &CRDYBOOT_VAR_VENDOR, None, None, &mut size),
            Status::BUFFER_TOO_SMALL
        );
        assert_eq!(size, 4);

        size = out.len();
        assert_eq!(
            store.get(
                name,
                &CRDYBOOT_VAR_VENDOR,
                Some(&mut got_attrs),
                Some(&mut out),
                &mut size
            ),
            Status::SUCCESS
        );
        assert_eq!(size, 4);
        assert_eq!(got_attrs, attrs);
        assert_eq!(&out[..4], &[1, 0, 0, 0]);

        assert_eq!(
            store.set(name, &CRDYBOOT_VAR_VENDOR, 0x07, &[2, 0, 0, 0, 9]),
            Status::SUCCESS
        );
        size = out.len();
        assert_eq!(
            store.get(
                name,
                &CRDYBOOT_VAR_VENDOR,
                Some(&mut got_attrs),
                Some(&mut out),
                &mut size
            ),
            Status::SUCCESS
        );
        assert_eq!(got_attrs, 0x07);
        assert_eq!(size, 5);
        assert_eq!(&out[..5], &[2, 0, 0, 0, 9]);

        assert_eq!(
            store.set(name, &CRDYBOOT_VAR_VENDOR, 0, &[]),
            Status::SUCCESS
        );
        assert_eq!(
            store.set(name, &CRDYBOOT_VAR_VENDOR, 0, &[]),
            Status::NOT_FOUND
        );
        size = out.len();
        assert_eq!(
            store.get(name, &CRDYBOOT_VAR_VENDOR, None, Some(&mut out), &mut size),
            Status::NOT_FOUND
        );
    }

    #[test]
    fn log_sink_accepts_large_payloads_without_consuming_slots() {
        let mut store = ShadowStore::new();
        let big = [b'A'; 16 * 1024];

        assert_eq!(
            store.set(&CRDYBOOT_LOG_NAME, &CRDYBOOT_VAR_VENDOR, 0x06, &big),
            Status::SUCCESS
        );
        assert_eq!(
            store.set(&CRDYSHIM_LOG_NAME, &CRDYBOOT_VAR_VENDOR, 0x06, &big),
            Status::SUCCESS
        );

        let mut size = 16usize;
        let mut buf = [0u8; 16];
        assert_eq!(
            store.get(
                &CRDYBOOT_LOG_NAME,
                &CRDYBOOT_VAR_VENDOR,
                None,
                Some(&mut buf),
                &mut size
            ),
            Status::NOT_FOUND
        );
    }

    #[test]
    fn vendor_filter_and_capacity_limits() {
        let mut store = ShadowStore::new();
        assert!(is_intercepted_vendor(&CRDYBOOT_VAR_VENDOR));
        assert!(is_intercepted_vendor(&SBAT_VAR_VENDOR));
        assert!(is_intercepted_vendor(&FWUPDATE_VENDOR));
        let other = Guid::new(1, 2, 3, [4; 8]);
        assert!(!is_intercepted_vendor(&other));

        let mut i = 0u8;
        while usize::from(i) < MAX_ENTRIES {
            let name = [u16::from(b'A' + i)];
            assert_eq!(
                store.set(&name, &SBAT_VAR_VENDOR, 0x03, &[i]),
                Status::SUCCESS
            );
            i += 1;
        }

        let extra = [u16::from(b'Z')];
        assert_eq!(
            store.set(&extra, &SBAT_VAR_VENDOR, 0x03, &[99]),
            Status::OUT_OF_RESOURCES
        );
    }
}
