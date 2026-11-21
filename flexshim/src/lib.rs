// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

#![cfg_attr(not(test), no_std)]

pub mod config;
pub mod crc32;
pub mod devpath;
pub mod hooks;
pub mod loader;
pub mod shadow;
pub mod uefi;
