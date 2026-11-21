// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

pub const DEVPATH_TYPE_MEDIA: u8 = 0x04;
pub const DEVPATH_SUBTYPE_FILE_PATH: u8 = 0x04;
pub const DEVPATH_TYPE_END: u8 = 0x7F;
pub const DEVPATH_SUBTYPE_END_ENTIRE: u8 = 0xFF;

/// Returns the byte offset of the `END_ENTIRE` node in a serialized UEFI device path.
///
/// Walking stops immediately on truncated or zero-length nodes to guard against
/// malformed firmware device paths.
pub fn prefix_len(dp_bytes: &[u8]) -> Option<usize> {
    let mut offset = 0usize;
    while offset + 4 <= dp_bytes.len() {
        let r#type = dp_bytes[offset];
        let sub_type = dp_bytes[offset + 1];
        let node_len = usize::from(u16::from_le_bytes([
            dp_bytes[offset + 2],
            dp_bytes[offset + 3],
        ]));
        if node_len < 4 {
            return None;
        }
        let next = offset.checked_add(node_len)?;
        if next > dp_bytes.len() {
            return None;
        }
        if r#type == DEVPATH_TYPE_END && sub_type == DEVPATH_SUBTYPE_END_ENTIRE {
            return Some(offset);
        }
        offset = next;
    }
    None
}

/// Computes the exact byte length of a device path with an appended file path node.
pub fn total_file_dp_len(prefix_bytes_len: usize, path_utf16_with_nul: &[u16]) -> Option<usize> {
    if path_utf16_with_nul.is_empty() {
        return None;
    }
    let path_bytes = path_utf16_with_nul.len().checked_mul(2)?;
    let file_node_len = 4usize.checked_add(path_bytes)?;
    if u16::try_from(file_node_len).is_err() {
        return None;
    }
    prefix_bytes_len.checked_add(file_node_len)?.checked_add(4)
}

/// Serializes `prefix` followed by a Media File Path node and an `END_ENTIRE` node.
pub fn write_file_device_path(
    prefix: &[u8],
    path_utf16_with_nul: &[u16],
    out: &mut [u8],
) -> Option<usize> {
    let total = total_file_dp_len(prefix.len(), path_utf16_with_nul)?;
    if out.len() < total {
        return None;
    }

    let file_node_len = 4 + path_utf16_with_nul.len() * 2;
    let file_node_len_le = u16::try_from(file_node_len).ok()?.to_le_bytes();

    out[..prefix.len()].copy_from_slice(prefix);

    let mut pos = prefix.len();
    out[pos] = DEVPATH_TYPE_MEDIA;
    out[pos + 1] = DEVPATH_SUBTYPE_FILE_PATH;
    out[pos + 2] = file_node_len_le[0];
    out[pos + 3] = file_node_len_le[1];
    pos += 4;

    for &unit in path_utf16_with_nul {
        let bytes = unit.to_le_bytes();
        out[pos] = bytes[0];
        out[pos + 1] = bytes[1];
        pos += 2;
    }

    out[pos] = DEVPATH_TYPE_END;
    out[pos + 1] = DEVPATH_SUBTYPE_END_ENTIRE;
    out[pos + 2] = 4;
    out[pos + 3] = 0;

    Some(total)
}

/// Resolves the path of a sibling file in the same directory as `file_dp_bytes`.
pub fn sibling_file_path(
    file_dp_bytes: &[u8],
    sibling_utf16_with_nul: &[u16],
    out: &mut [u16],
) -> Option<usize> {
    if sibling_utf16_with_nul.is_empty() || *sibling_utf16_with_nul.last()? != 0 {
        return None;
    }

    let end_offset = prefix_len(file_dp_bytes)?;
    let mut offset = 0usize;
    let mut pos = 0usize;
    let mut saw_filepath_node = false;
    let bs_unit = u16::from(b'\\');
    let sl_unit = u16::from(b'/');

    while offset < end_offset {
        let r#type = file_dp_bytes[offset];
        let sub_type = file_dp_bytes[offset + 1];
        let node_len = usize::from(u16::from_le_bytes([
            file_dp_bytes[offset + 2],
            file_dp_bytes[offset + 3],
        ]));
        if r#type == DEVPATH_TYPE_MEDIA && sub_type == DEVPATH_SUBTYPE_FILE_PATH {
            saw_filepath_node = true;
            let payload = &file_dp_bytes[offset + 4..offset + node_len];
            let mut node_started = false;
            for pair in payload.chunks_exact(2) {
                let raw = u16::from_le_bytes([pair[0], pair[1]]);
                if raw == 0 {
                    break;
                }
                let unit = if raw == sl_unit { bs_unit } else { raw };
                if !node_started {
                    node_started = true;
                    if pos == 0 {
                        if unit != bs_unit {
                            if pos >= out.len() {
                                return None;
                            }
                            out[pos] = bs_unit;
                            pos += 1;
                        }
                    } else if out[pos - 1] == bs_unit && unit == bs_unit {
                        continue;
                    } else if out[pos - 1] != bs_unit && unit != bs_unit {
                        if pos >= out.len() {
                            return None;
                        }
                        out[pos] = bs_unit;
                        pos += 1;
                    }
                } else if unit == bs_unit && pos > 0 && out[pos - 1] == bs_unit {
                    continue;
                }
                if pos >= out.len() {
                    return None;
                }
                out[pos] = unit;
                pos += 1;
            }
        }
        offset += node_len;
    }

    if !saw_filepath_node || pos == 0 {
        return None;
    }

    let last_bs = out[..pos].iter().rposition(|&u| u == bs_unit)?;
    let dir_len = last_bs + 1;
    let total = dir_len.checked_add(sibling_utf16_with_nul.len())?;
    if total > out.len() {
        return None;
    }
    out[dir_len..total].copy_from_slice(sibling_utf16_with_nul);
    Some(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_filepath_dp(nodes: &[&str]) -> [u8; 256] {
        let mut buf = [0u8; 256];
        let mut pos = 0usize;
        for &node in nodes {
            let start = pos;
            pos += 4;
            for u in node.encode_utf16() {
                let b = u.to_le_bytes();
                buf[pos] = b[0];
                buf[pos + 1] = b[1];
                pos += 2;
            }
            buf[pos] = 0;
            buf[pos + 1] = 0;
            pos += 2;
            let len_le = u16::try_from(pos - start).unwrap().to_le_bytes();
            buf[start] = DEVPATH_TYPE_MEDIA;
            buf[start + 1] = DEVPATH_SUBTYPE_FILE_PATH;
            buf[start + 2] = len_le[0];
            buf[start + 3] = len_le[1];
        }
        buf[pos] = DEVPATH_TYPE_END;
        buf[pos + 1] = DEVPATH_SUBTYPE_END_ENTIRE;
        buf[pos + 2] = 4;
        buf[pos + 3] = 0;
        buf
    }

    fn assert_utf16_path(actual: &[u16], expected_with_nul: &str) {
        let mut expected = [0u16; 128];
        let mut len = 0usize;
        for u in expected_with_nul.encode_utf16() {
            expected[len] = u;
            len += 1;
        }
        assert_eq!(actual, &expected[..len]);
    }

    #[test]
    fn append_file_path_to_multi_node_prefix() {
        let raw_dp: [u8; 16] = [
            0x02, 0x01, 0x06, 0x00, 0x00, 0x01, // ACPI node (len 6)
            0x04, 0x01, 0x06, 0x00, 0x02, 0x00, // HardDrive node (len 6)
            0x7F, 0xFF, 0x04, 0x00, // End Entire
        ];
        let plen = prefix_len(&raw_dp).unwrap();
        assert_eq!(plen, 12);

        let path: [u16; 6] = [
            b'\\' as u16,
            b'e' as u16,
            b'f' as u16,
            b'i' as u16,
            b'\\' as u16,
            0,
        ];
        let total = total_file_dp_len(plen, &path).unwrap();
        assert_eq!(total, 12 + (4 + 12) + 4);

        let mut out = [0u8; 64];
        let written = write_file_device_path(&raw_dp[..plen], &path, &mut out).unwrap();
        assert_eq!(written, total);
        assert_eq!(&out[..12], &raw_dp[..12]);
        assert_eq!(&out[12..16], &[0x04, 0x04, 16, 0]);
        assert_eq!(&out[28..32], &[0x7F, 0xFF, 0x04, 0x00]);
        assert_eq!(prefix_len(&out[..written]), Some(28));
    }

    #[test]
    fn rejects_malformed_device_paths() {
        assert_eq!(prefix_len(&[]), None);
        assert_eq!(prefix_len(&[0x01, 0x01, 0x00, 0x00]), None);
        assert_eq!(prefix_len(&[0x01, 0x01, 0x08, 0x00, 0xAA, 0xBB]), None);
        assert_eq!(prefix_len(&[0x7F, 0x01, 0x04, 0x00]), None);
    }

    #[test]
    fn resolves_sibling_file_paths() {
        let sib: [u16; 13] = [
            b'f' as u16,
            b'l' as u16,
            b'e' as u16,
            b'x' as u16,
            b's' as u16,
            b'h' as u16,
            b'i' as u16,
            b'm' as u16,
            b'.' as u16,
            b'c' as u16,
            b'f' as u16,
            b'g' as u16,
            0,
        ];
        let mut out = [0u16; 128];

        let dp1 = make_filepath_dp(&["\\efi\\boot\\bootx64.efi"]);
        let n1 = sibling_file_path(&dp1, &sib, &mut out).unwrap();
        assert_utf16_path(&out[..n1], "\\efi\\boot\\flexshim.cfg\0");

        let dp2 = make_filepath_dp(&["\\EFI\\flexshim\\flexshimx64.efi"]);
        let n2 = sibling_file_path(&dp2, &sib, &mut out).unwrap();
        assert_utf16_path(&out[..n2], "\\EFI\\flexshim\\flexshim.cfg\0");

        let dp3 = make_filepath_dp(&["EFI", "flexshim", "flexshimx64.efi"]);
        let n3 = sibling_file_path(&dp3, &sib, &mut out).unwrap();
        assert_utf16_path(&out[..n3], "\\EFI\\flexshim\\flexshim.cfg\0");

        let dp4 = make_filepath_dp(&["\\flexshimx64.efi"]);
        let n4 = sibling_file_path(&dp4, &sib, &mut out).unwrap();
        assert_utf16_path(&out[..n4], "\\flexshim.cfg\0");

        let dp5 = make_filepath_dp(&["efi/boot/bootx64.efi"]);
        let n5 = sibling_file_path(&dp5, &sib, &mut out).unwrap();
        assert_utf16_path(&out[..n5], "\\efi\\boot\\flexshim.cfg\0");

        let non_file_dp: [u8; 10] = [0x04, 0x01, 0x06, 0x00, 0x01, 0x00, 0x7F, 0xFF, 0x04, 0x00];
        assert_eq!(sibling_file_path(&non_file_dp, &sib, &mut out), None);
        assert_eq!(sibling_file_path(&[], &sib, &mut out), None);
    }
}
