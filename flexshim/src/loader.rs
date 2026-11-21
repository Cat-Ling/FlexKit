// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

use crate::config::{Config, MAX_CONFIG_BYTES};
use crate::devpath::{
    self, DEVPATH_SUBTYPE_END_ENTIRE, DEVPATH_TYPE_END,
};
use crate::hooks;
use crate::uefi::{
    BootServices, DEVICE_PATH_PROTOCOL_GUID, DevicePathHeader, EFI_FILE_MODE_READ, FileProtocol,
    Handle, LOCATE_BY_PROTOCOL, LOADED_IMAGE_PROTOCOL_GUID, LoadedImageProtocol,
    MEMORY_TYPE_LOADER_DATA, SIMPLE_FILE_SYSTEM_PROTOCOL_GUID, SimpleFileSystemProtocol, Status,
    SystemTable, write_console,
};
use core::ffi::c_void;
use core::ptr;
use core::slice;

const MAX_DP_BYTES: usize = 16 * 1024;
const MAX_CFG_PATH_UNITS: usize = 256;

// `\efi\boot\crdybootx64.efi` + NUL
pub const TARGET_PATH_UTF16: [u16; 26] = [
    b'\\' as u16,
    b'e' as u16,
    b'f' as u16,
    b'i' as u16,
    b'\\' as u16,
    b'b' as u16,
    b'o' as u16,
    b'o' as u16,
    b't' as u16,
    b'\\' as u16,
    b'c' as u16,
    b'r' as u16,
    b'd' as u16,
    b'y' as u16,
    b'b' as u16,
    b'o' as u16,
    b'o' as u16,
    b't' as u16,
    b'x' as u16,
    b'6' as u16,
    b'4' as u16,
    b'.' as u16,
    b'e' as u16,
    b'f' as u16,
    b'i' as u16,
    0,
];

// `flexshim.cfg` + NUL
pub const CFG_NAME_UTF16: [u16; 13] = [
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

// `\efi\boot\flexshim.cfg` + NUL
pub const FALLBACK_CFG_PATH_UTF16: [u16; 23] = [
    b'\\' as u16,
    b'e' as u16,
    b'f' as u16,
    b'i' as u16,
    b'\\' as u16,
    b'b' as u16,
    b'o' as u16,
    b'o' as u16,
    b't' as u16,
    b'\\' as u16,
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

#[derive(Copy, Clone, Debug)]
pub struct ChainloadError {
    pub stage: &'static str,
    pub status: Status,
}

unsafe fn device_path_slice<'a>(dp_ptr: *const DevicePathHeader) -> Option<&'a [u8]> {
    if dp_ptr.is_null() {
        return None;
    }
    let base = dp_ptr.cast::<u8>();
    let mut offset = 0usize;
    while offset + 4 <= MAX_DP_BYTES {
        let hdr = unsafe { slice::from_raw_parts(base.add(offset), 4) };
        let node_len = usize::from(u16::from_le_bytes([hdr[2], hdr[3]]));
        if node_len < 4 || offset + node_len > MAX_DP_BYTES {
            return None;
        }
        offset += node_len;
        if hdr[0] == DEVPATH_TYPE_END && hdr[1] == DEVPATH_SUBTYPE_END_ENTIRE {
            return Some(unsafe { slice::from_raw_parts(base, offset) });
        }
    }
    None
}

unsafe fn open_root_and_file(
    bs: *mut BootServices,
    fs_handle: Handle,
    path_utf16: &[u16],
) -> Option<(*mut FileProtocol, *mut FileProtocol)> {
    let mut sfs_ptr: *mut c_void = ptr::null_mut();
    let status = unsafe {
        ((*bs).handle_protocol)(
            fs_handle,
            &SIMPLE_FILE_SYSTEM_PROTOCOL_GUID,
            &raw mut sfs_ptr,
        )
    };
    if status.is_error() || sfs_ptr.is_null() {
        return None;
    }
    let sfs = sfs_ptr.cast::<SimpleFileSystemProtocol>();

    let mut root: *mut FileProtocol = ptr::null_mut();
    let status = unsafe { ((*sfs).open_volume)(sfs, &raw mut root) };
    if status.is_error() || root.is_null() {
        return None;
    }

    let mut file: *mut FileProtocol = ptr::null_mut();
    let open_status = unsafe {
        ((*root).open)(
            root,
            &raw mut file,
            path_utf16.as_ptr(),
            EFI_FILE_MODE_READ,
            0,
        )
    };
    if open_status.is_error() || file.is_null() {
        unsafe { ((*root).close)(root) };
        return None;
    }
    Some((root, file))
}

unsafe fn file_exists_on_fs(bs: *mut BootServices, fs_handle: Handle, path_utf16: &[u16]) -> bool {
    let Some((root, file)) = (unsafe { open_root_and_file(bs, fs_handle, path_utf16) }) else {
        return false;
    };
    unsafe {
        ((*file).close)(file);
        ((*root).close)(root);
    }
    true
}

/// Reads optional `flexshim.cfg` from the shim's directory, returning defaults if missing.
///
/// # Safety
/// `image_handle` and `st` must be valid UEFI handles/tables provided to `efi_main`.
pub unsafe fn load_config(image_handle: Handle, st: *mut SystemTable) -> Config {
    if st.is_null() {
        return Config::new();
    }
    let bs = unsafe { (*st).boot_services };
    if bs.is_null() {
        return Config::new();
    }

    let mut li_ptr: *mut c_void = ptr::null_mut();
    let li_status = unsafe {
        ((*bs).handle_protocol)(
            image_handle,
            &LOADED_IMAGE_PROTOCOL_GUID,
            &raw mut li_ptr,
        )
    };
    if li_status.is_error() || li_ptr.is_null() {
        return Config::new();
    }

    let li = li_ptr.cast::<LoadedImageProtocol>();
    let device_handle = unsafe { (*li).device_handle };
    if device_handle.is_null() {
        return Config::new();
    }

    let mut path_buf = [0u16; MAX_CFG_PATH_UNITS];
    let cfg_path: &[u16] = match unsafe { device_path_slice((*li).file_path) }
        .and_then(|raw| devpath::sibling_file_path(raw, &CFG_NAME_UTF16, &mut path_buf))
    {
        Some(len) => &path_buf[..len],
        None => &FALLBACK_CFG_PATH_UTF16,
    };

    let Some((root, file)) = (unsafe { open_root_and_file(bs, device_handle, cfg_path) }) else {
        return Config::new();
    };

    let mut raw = [0u8; MAX_CONFIG_BYTES];
    let mut total = 0usize;
    while total < raw.len() {
        let mut chunk_len = raw.len() - total;
        let read_status =
            unsafe { ((*file).read)(file, &raw mut chunk_len, raw.as_mut_ptr().add(total)) };
        if read_status.is_error() || chunk_len == 0 {
            break;
        }
        total += chunk_len;
    }

    unsafe {
        ((*file).close)(file);
        ((*root).close)(root);
    }

    let cfg = Config::parse(&raw[..total]);
    if cfg.warnings > 0 {
        unsafe {
            write_console(st, "flexshim: ignored invalid line(s) in flexshim.cfg\n");
        }
    }
    cfg
}

unsafe fn find_target_fs_handle(
    image_handle: Handle,
    bs: *mut BootServices,
) -> Result<Handle, ChainloadError> {
    let mut self_device: Handle = ptr::null_mut();
    let mut li_ptr: *mut c_void = ptr::null_mut();
    let li_status = unsafe {
        ((*bs).handle_protocol)(
            image_handle,
            &LOADED_IMAGE_PROTOCOL_GUID,
            &raw mut li_ptr,
        )
    };
    if li_status.is_success() && !li_ptr.is_null() {
        let li = li_ptr.cast::<LoadedImageProtocol>();
        self_device = unsafe { (*li).device_handle };
        // Prefer the boot medium holding this shim when multiple Flex disks are attached.
        if !self_device.is_null()
            && unsafe { file_exists_on_fs(bs, self_device, &TARGET_PATH_UTF16) }
        {
            return Ok(self_device);
        }
    }

    let mut count = 0usize;
    let mut handles: *mut Handle = ptr::null_mut();
    let status = unsafe {
        ((*bs).locate_handle_buffer)(
            LOCATE_BY_PROTOCOL,
            &SIMPLE_FILE_SYSTEM_PROTOCOL_GUID,
            ptr::null(),
            &raw mut count,
            &raw mut handles,
        )
    };
    if status.is_error() {
        return Err(ChainloadError {
            stage: "LocateHandleBuffer(SimpleFileSystem)",
            status,
        });
    }

    let mut found: Option<Handle> = None;
    if !handles.is_null() && count > 0 {
        let handle_slice = unsafe { slice::from_raw_parts(handles, count) };
        for &h in handle_slice {
            if h.is_null() || h == self_device {
                continue;
            }
            if unsafe { file_exists_on_fs(bs, h, &TARGET_PATH_UTF16) } {
                found = Some(h);
                break;
            }
        }
    }

    if !handles.is_null() {
        unsafe { ((*bs).free_pool)(handles.cast()) };
    }

    found.ok_or(ChainloadError {
        stage: "Locate crdybootx64.efi",
        status: Status::NOT_FOUND,
    })
}

unsafe fn build_target_device_path(
    bs: *mut BootServices,
    fs_handle: Handle,
) -> Result<*mut DevicePathHeader, ChainloadError> {
    let mut dp_ptr: *mut c_void = ptr::null_mut();
    let status = unsafe {
        ((*bs).handle_protocol)(
            fs_handle,
            &DEVICE_PATH_PROTOCOL_GUID,
            &raw mut dp_ptr,
        )
    };
    if status.is_error() || dp_ptr.is_null() {
        return Err(ChainloadError {
            stage: "HandleProtocol(DevicePath)",
            status: if status.is_error() {
                status
            } else {
                Status::NOT_FOUND
            },
        });
    }

    let raw_slice = unsafe { device_path_slice(dp_ptr.cast()) }.ok_or(ChainloadError {
        stage: "Parse DevicePath",
        status: Status::INVALID_PARAMETER,
    })?;
    let plen = devpath::prefix_len(raw_slice).ok_or(ChainloadError {
        stage: "Parse DevicePath prefix",
        status: Status::INVALID_PARAMETER,
    })?;

    let total_len = devpath::total_file_dp_len(plen, &TARGET_PATH_UTF16).ok_or(ChainloadError {
        stage: "Compute DevicePath length",
        status: Status::INVALID_PARAMETER,
    })?;

    let mut pool_buf: *mut u8 = ptr::null_mut();
    let alloc_status =
        unsafe { ((*bs).allocate_pool)(MEMORY_TYPE_LOADER_DATA, total_len, &raw mut pool_buf) };
    if alloc_status.is_error() || pool_buf.is_null() {
        return Err(ChainloadError {
            stage: "AllocatePool(DevicePath)",
            status: if alloc_status.is_error() {
                alloc_status
            } else {
                Status::OUT_OF_RESOURCES
            },
        });
    }

    let out_slice = unsafe { slice::from_raw_parts_mut(pool_buf, total_len) };
    if devpath::write_file_device_path(&raw_slice[..plen], &TARGET_PATH_UTF16, out_slice).is_none()
    {
        unsafe { ((*bs).free_pool)(pool_buf) };
        return Err(ChainloadError {
            stage: "Write DevicePath",
            status: Status::INVALID_PARAMETER,
        });
    }

    Ok(pool_buf.cast())
}

/// Locates `\efi\boot\crdybootx64.efi` and transfers control via `LoadImage` + `StartImage`.
///
/// # Safety
/// `image_handle` and `st` must be valid UEFI handles/tables provided to `efi_main`.
pub unsafe fn chainload_crdyboot(
    image_handle: Handle,
    st: *mut SystemTable,
) -> Result<(), ChainloadError> {
    if st.is_null() {
        return Err(ChainloadError {
            stage: "SystemTable check",
            status: Status::INVALID_PARAMETER,
        });
    }
    let bs = unsafe { (*st).boot_services };
    if bs.is_null() {
        return Err(ChainloadError {
            stage: "BootServices check",
            status: Status::INVALID_PARAMETER,
        });
    }

    let target_fs = unsafe { find_target_fs_handle(image_handle, bs) }?;
    let full_dp = unsafe { build_target_device_path(bs, target_fs) }?;

    let mut child_handle: Handle = ptr::null_mut();
    let load_status = unsafe {
        ((*bs).load_image)(
            0,
            image_handle,
            full_dp,
            ptr::null(),
            0,
            &raw mut child_handle,
        )
    };
    unsafe { ((*bs).free_pool)(full_dp.cast()) };

    if load_status.is_error() {
        return Err(ChainloadError {
            stage: "LoadImage",
            status: load_status,
        });
    }

    unsafe { hooks::set_child_image_handle(child_handle) };

    let mut exit_data_size = 0usize;
    let mut exit_data: *mut u16 = ptr::null_mut();
    let start_status =
        unsafe { ((*bs).start_image)(child_handle, &raw mut exit_data_size, &raw mut exit_data) };
    unsafe { hooks::set_child_image_handle(ptr::null_mut()) };
    if !exit_data.is_null() {
        unsafe { ((*bs).free_pool)(exit_data.cast()) };
    }

    if start_status.is_error() {
        return Err(ChainloadError {
            stage: "StartImage",
            status: start_status,
        });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_path_matches_utf16_literal() {
        assert!(
            TARGET_PATH_UTF16
                .iter()
                .copied()
                .eq("\\efi\\boot\\crdybootx64.efi\0".encode_utf16())
        );
        assert!(
            CFG_NAME_UTF16
                .iter()
                .copied()
                .eq("flexshim.cfg\0".encode_utf16())
        );
        assert!(
            FALLBACK_CFG_PATH_UTF16
                .iter()
                .copied()
                .eq("\\efi\\boot\\flexshim.cfg\0".encode_utf16())
        );
    }
}
