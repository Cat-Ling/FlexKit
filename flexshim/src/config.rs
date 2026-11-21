// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

pub const MAX_CONFIG_BYTES: usize = 4096;
pub const MAX_EXTRA_CMDLINE_BYTES: usize = 512;
pub const MAX_CMDLINE_UNITS: usize = 4096;

const CROS_DEBUG_TOKEN: &[u8] = b"cros_debug";

#[derive(Copy, Clone)]
pub struct Config {
    pub block_updates: bool,
    extra_len: usize,
    extra_cmdline: [u8; MAX_EXTRA_CMDLINE_BYTES],
    pub warnings: u32,
}

impl Config {
    pub const fn new() -> Self {
        Self {
            block_updates: false,
            extra_len: 0,
            extra_cmdline: [0; MAX_EXTRA_CMDLINE_BYTES],
            warnings: 0,
        }
    }

    pub fn extra_cmdline(&self) -> &[u8] {
        &self.extra_cmdline[..self.extra_len]
    }

    pub fn has_cmdline_additions(&self) -> bool {
        self.block_updates || self.extra_len > 0
    }

    pub fn parse(raw: &[u8]) -> Self {
        let mut cfg = Self::new();
        let data = strip_utf8_bom(raw);

        for raw_line in data.split(|&b| b == b'\n') {
            let line = trim_ascii_ws(strip_trailing_cr(raw_line));
            if line.is_empty() || line[0] == b'#' {
                continue;
            }

            let Some(eq_idx) = line.iter().position(|&b| b == b'=') else {
                cfg.warnings = cfg.warnings.saturating_add(1);
                continue;
            };

            let key = trim_ascii_ws(&line[..eq_idx]);
            let val = trim_ascii_ws(&line[eq_idx + 1..]);

            if key == b"block_updates" {
                if eq_ignore_ascii_case(val, b"true")
                    || val == b"1"
                    || eq_ignore_ascii_case(val, b"yes")
                {
                    cfg.block_updates = true;
                } else if eq_ignore_ascii_case(val, b"false")
                    || val == b"0"
                    || eq_ignore_ascii_case(val, b"no")
                {
                    cfg.block_updates = false;
                } else {
                    cfg.warnings = cfg.warnings.saturating_add(1);
                }
            } else if key == b"extra_cmdline" {
                cfg.extra_len = 0;
                parse_extra_cmdline(val, &mut cfg);
            } else {
                cfg.warnings = cfg.warnings.saturating_add(1);
            }
        }

        cfg
    }
}

impl Default for Config {
    fn default() -> Self {
        Self::new()
    }
}

fn strip_utf8_bom(bytes: &[u8]) -> &[u8] {
    if bytes.len() >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF {
        &bytes[3..]
    } else {
        bytes
    }
}

fn strip_trailing_cr(line: &[u8]) -> &[u8] {
    if let Some((&b'\r', rest)) = line.split_last() {
        rest
    } else {
        line
    }
}

fn is_ascii_ws(b: u8) -> bool {
    b == b' ' || b == b'\t'
}

fn trim_ascii_ws(mut s: &[u8]) -> &[u8] {
    while let Some((&first, rest)) = s.split_first() {
        if is_ascii_ws(first) {
            s = rest;
        } else {
            break;
        }
    }
    while let Some((&last, rest)) = s.split_last() {
        if is_ascii_ws(last) {
            s = rest;
        } else {
            break;
        }
    }
    s
}

fn eq_ignore_ascii_case(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut i = 0;
    while i < a.len() {
        if a[i].to_ascii_lowercase() != b[i].to_ascii_lowercase() {
            return false;
        }
        i += 1;
    }
    true
}

fn parse_extra_cmdline(val: &[u8], cfg: &mut Config) {
    let mut i = 0usize;
    while i < val.len() {
        while i < val.len() && is_ascii_ws(val[i]) {
            i += 1;
        }
        if i >= val.len() {
            break;
        }
        let start = i;
        while i < val.len() && !is_ascii_ws(val[i]) {
            i += 1;
        }
        let token = &val[start..i];
        if !token.iter().all(|&b| (0x21..=0x7E).contains(&b)) {
            cfg.warnings = cfg.warnings.saturating_add(1);
            continue;
        }

        let sep = usize::from(cfg.extra_len > 0);
        let needed = match cfg.extra_len.checked_add(sep).and_then(|n| n.checked_add(token.len())) {
            Some(n) => n,
            None => {
                cfg.warnings = cfg.warnings.saturating_add(1);
                break;
            }
        };
        if needed > MAX_EXTRA_CMDLINE_BYTES {
            cfg.warnings = cfg.warnings.saturating_add(1);
            break;
        }

        if sep == 1 {
            cfg.extra_cmdline[cfg.extra_len] = b' ';
            cfg.extra_len += 1;
        }
        cfg.extra_cmdline[cfg.extra_len..cfg.extra_len + token.len()].copy_from_slice(token);
        cfg.extra_len += token.len();
    }
}

fn contains_ascii_token(utf16: &[u16], token: &[u8]) -> bool {
    let mut i = 0usize;
    while i < utf16.len() {
        while i < utf16.len() && (utf16[i] == u16::from(b' ') || utf16[i] == u16::from(b'\t')) {
            i += 1;
        }
        let start = i;
        while i < utf16.len() && utf16[i] != u16::from(b' ') && utf16[i] != u16::from(b'\t') {
            i += 1;
        }
        let word = &utf16[start..i];
        if word.len() == token.len() {
            let mut matched = true;
            let mut j = 0usize;
            while j < token.len() {
                if word[j] != u16::from(token[j]) {
                    matched = false;
                    break;
                }
                j += 1;
            }
            if matched {
                return true;
            }
        }
    }
    false
}

fn append_ascii_token(out: &mut [u16], pos: &mut usize, token: &[u8]) -> bool {
    let sep = usize::from(*pos > 0);
    let Some(end) = pos
        .checked_add(sep)
        .and_then(|n| n.checked_add(token.len()))
        .and_then(|n| n.checked_add(1))
    else {
        return false;
    };
    if end > out.len() {
        return false;
    }
    if sep == 1 {
        out[*pos] = u16::from(b' ');
        *pos += 1;
    }
    for &b in token {
        out[*pos] = u16::from(b);
        *pos += 1;
    }
    true
}

/// Appends configured kernel options onto `existing_utf16` into `out` with a trailing NUL.
///
/// Strips any trailing NUL and ASCII spaces from `existing_utf16` so appended
/// options remain part of the command line read by the Linux EFI stub.
pub fn build_merged_cmdline(existing_utf16: &[u16], cfg: &Config, out: &mut [u16]) -> Option<usize> {
    let nul_pos = existing_utf16
        .iter()
        .position(|&u| u == 0)
        .unwrap_or(existing_utf16.len());
    let mut base = &existing_utf16[..nul_pos];
    while let Some((&last, rest)) = base.split_last() {
        if last == u16::from(b' ') {
            base = rest;
        } else {
            break;
        }
    }

    if base.len().checked_add(1)? > out.len() {
        return None;
    }
    out[..base.len()].copy_from_slice(base);
    let mut pos = base.len();

    let extra = cfg.extra_cmdline();
    let mut i = 0usize;
    while i < extra.len() {
        while i < extra.len() && is_ascii_ws(extra[i]) {
            i += 1;
        }
        if i >= extra.len() {
            break;
        }
        let start = i;
        while i < extra.len() && !is_ascii_ws(extra[i]) {
            i += 1;
        }
        let token = &extra[start..i];
        if token == CROS_DEBUG_TOKEN && contains_ascii_token(&out[..pos], CROS_DEBUG_TOKEN) {
            continue;
        }
        if !append_ascii_token(out, &mut pos, token) {
            return None;
        }
    }

    if cfg.block_updates && !contains_ascii_token(&out[..pos], CROS_DEBUG_TOKEN) {
        if !append_ascii_token(out, &mut pos, CROS_DEBUG_TOKEN) {
            return None;
        }
    }

    if pos >= out.len() {
        return None;
    }
    out[pos] = 0;
    Some(pos + 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn to_utf16_with_nul(s: &str) -> [u16; 128] {
        let mut buf = [0u16; 128];
        let mut i = 0usize;
        for u in s.encode_utf16() {
            buf[i] = u;
            i += 1;
        }
        buf[i] = 0;
        buf
    }

    fn assert_utf16_eq(actual: &[u16], expected_with_nul: &str) {
        let mut expected = [0u16; 256];
        let mut len = 0usize;
        for u in expected_with_nul.encode_utf16() {
            expected[len] = u;
            len += 1;
        }
        assert_eq!(actual, &expected[..len]);
    }

    #[test]
    fn commented_default_config_has_no_additions() {
        let raw = b"\
# block_updates=true\n\
# extra_cmdline=i8042.nomux\n\
";
        let cfg = Config::parse(raw);
        assert!(!cfg.block_updates);
        assert!(cfg.extra_cmdline().is_empty());
        assert!(!cfg.has_cmdline_additions());
        assert_eq!(cfg.warnings, 0);
    }

    #[test]
    fn parses_bom_crlf_whitespace_and_values() {
        let mut raw = [0u8; 128];
        let body = b"\xEF\xBB\xBF  block_updates = YES \r\n\t# comment\r\n extra_cmdline =  i8042.nomux   i915.enable_psr=0 \r\n";
        raw[..body.len()].copy_from_slice(body);
        let cfg = Config::parse(&raw[..body.len()]);
        assert!(cfg.block_updates);
        assert_eq!(cfg.extra_cmdline(), b"i8042.nomux i915.enable_psr=0");
        assert!(cfg.has_cmdline_additions());
        assert_eq!(cfg.warnings, 0);
    }

    #[test]
    fn unknown_keys_and_bad_values_increment_warnings() {
        let raw = b"missing_equals\nunknown_key=1\nblock_updates=maybe\nblock_updates=true\nextra_cmdline=ok_flag bad\x01flag next_flag\n";
        let cfg = Config::parse(raw);
        assert!(cfg.block_updates);
        assert_eq!(cfg.extra_cmdline(), b"ok_flag next_flag");
        assert_eq!(cfg.warnings, 4);
    }

    #[test]
    fn merge_strips_nul_appends_flags_and_dedups_cros_debug() {
        let base_buf = to_utf16_with_nul("quiet cros_efi   ");
        let base_len = "quiet cros_efi   ".len() + 1;
        let cfg = Config::parse(b"block_updates=true\nextra_cmdline=cros_debug i8042.nomux\n");

        let mut out = [0xFFFFu16; 128];
        let merged_len = build_merged_cmdline(&base_buf[..base_len], &cfg, &mut out).unwrap();
        assert_utf16_eq(
            &out[..merged_len],
            "quiet cros_efi cros_debug i8042.nomux\0",
        );

        let mut tiny = [0u16; 20];
        assert_eq!(build_merged_cmdline(&base_buf[..base_len], &cfg, &mut tiny), None);
    }
}
