// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

use core::ffi::c_void;
use core::mem::offset_of;

pub type Handle = *mut c_void;
pub type Event = *mut c_void;
pub type Tpl = usize;

pub const TPL_CALLBACK: Tpl = 8;
pub const TPL_NOTIFY: Tpl = 16;

pub const EVT_SIGNAL_EXIT_BOOT_SERVICES: u32 = 0x0000_0201;

pub const EFI_FILE_MODE_READ: u64 = 1;
pub const MEMORY_TYPE_LOADER_DATA: u32 = 2;
pub const LOCATE_BY_PROTOCOL: u32 = 2;

const ERROR_BIT: usize = 1usize << 63;

#[repr(transparent)]
#[derive(Copy, Clone, Eq, PartialEq, Debug)]
pub struct Status(pub usize);

impl Status {
    pub const SUCCESS: Self = Self(0);
    pub const LOAD_ERROR: Self = Self(ERROR_BIT | 1);
    pub const INVALID_PARAMETER: Self = Self(ERROR_BIT | 2);
    pub const UNSUPPORTED: Self = Self(ERROR_BIT | 3);
    pub const BAD_BUFFER_SIZE: Self = Self(ERROR_BIT | 4);
    pub const BUFFER_TOO_SMALL: Self = Self(ERROR_BIT | 5);
    pub const OUT_OF_RESOURCES: Self = Self(ERROR_BIT | 9);
    pub const NOT_FOUND: Self = Self(ERROR_BIT | 14);

    pub const fn is_success(self) -> bool {
        self.0 == 0
    }

    pub const fn is_error(self) -> bool {
        (self.0 & ERROR_BIT) != 0
    }
}

#[repr(C)]
#[derive(Copy, Clone, Eq, PartialEq, Debug)]
pub struct Guid {
    pub data1: u32,
    pub data2: u16,
    pub data3: u16,
    pub data4: [u8; 8],
}

impl Guid {
    pub const fn new(data1: u32, data2: u16, data3: u16, data4: [u8; 8]) -> Self {
        Self {
            data1,
            data2,
            data3,
            data4,
        }
    }
}

pub const LOADED_IMAGE_PROTOCOL_GUID: Guid = Guid::new(
    0x5B1B_31A1,
    0x9562,
    0x11D2,
    [0x8E, 0x3F, 0x00, 0xA0, 0xC9, 0x69, 0x72, 0x3B],
);

pub const DEVICE_PATH_PROTOCOL_GUID: Guid = Guid::new(
    0x0957_6E91,
    0x6D3F,
    0x11D2,
    [0x8E, 0x39, 0x00, 0xA0, 0xC9, 0x69, 0x72, 0x3B],
);

pub const SIMPLE_FILE_SYSTEM_PROTOCOL_GUID: Guid = Guid::new(
    0x964E_5B22,
    0x6459,
    0x11D2,
    [0x8E, 0x39, 0x00, 0xA0, 0xC9, 0x69, 0x72, 0x3B],
);

pub const CRDYBOOT_VAR_VENDOR: Guid = Guid::new(
    0x2A6F_93C9,
    0x29EA,
    0x46BF,
    [0xB6, 0x18, 0x27, 0x1B, 0x63, 0xBA, 0xAC, 0xF3],
);

pub const SBAT_VAR_VENDOR: Guid = Guid::new(
    0x605D_AB50,
    0xE046,
    0x4300,
    [0xAB, 0xB6, 0x3D, 0xD8, 0x10, 0xDD, 0x8B, 0x23],
);

pub const FWUPDATE_VENDOR: Guid = Guid::new(
    0x0ABB_A7DC,
    0xE516,
    0x4167,
    [0xBB, 0xF5, 0x4D, 0x9D, 0x1C, 0x73, 0x94, 0x16],
);

#[repr(C)]
#[derive(Copy, Clone, Debug)]
pub struct TableHeader {
    pub signature: u64,
    pub revision: u32,
    pub header_size: u32,
    pub crc32: u32,
    pub reserved: u32,
}

pub type GetVariableFn = unsafe extern "efiapi" fn(
    variable_name: *const u16,
    vendor_guid: *const Guid,
    attributes: *mut u32,
    data_size: *mut usize,
    data: *mut u8,
) -> Status;

pub type SetVariableFn = unsafe extern "efiapi" fn(
    variable_name: *const u16,
    vendor_guid: *const Guid,
    attributes: u32,
    data_size: usize,
    data: *const u8,
) -> Status;

pub type EventNotifyFn = unsafe extern "efiapi" fn(event: Event, context: *mut c_void);

#[repr(C)]
pub struct RuntimeServices {
    pub hdr: TableHeader,
    pub get_time: *const c_void,
    pub set_time: *const c_void,
    pub get_wakeup_time: *const c_void,
    pub set_wakeup_time: *const c_void,
    pub set_virtual_address_map: *const c_void,
    pub convert_pointer: *const c_void,
    pub get_variable: GetVariableFn,
    pub get_next_variable_name: *const c_void,
    pub set_variable: SetVariableFn,
    pub get_next_high_monotonic_count: *const c_void,
    pub reset_system: *const c_void,
    pub update_capsule: *const c_void,
    pub query_capsule_capabilities: *const c_void,
    pub query_variable_info: *const c_void,
}

#[repr(C)]
pub struct BootServices {
    pub hdr: TableHeader,
    pub raise_tpl: *const c_void,
    pub restore_tpl: *const c_void,
    pub allocate_pages: *const c_void,
    pub free_pages: *const c_void,
    pub get_memory_map: *const c_void,
    pub allocate_pool:
        unsafe extern "efiapi" fn(pool_type: u32, size: usize, buffer: *mut *mut u8) -> Status,
    pub free_pool: unsafe extern "efiapi" fn(buffer: *mut u8) -> Status,
    pub create_event: unsafe extern "efiapi" fn(
        r#type: u32,
        notify_tpl: Tpl,
        notify_function: Option<EventNotifyFn>,
        notify_context: *mut c_void,
        event: *mut Event,
    ) -> Status,
    pub set_timer: *const c_void,
    pub wait_for_event: *const c_void,
    pub signal_event: *const c_void,
    pub close_event: unsafe extern "efiapi" fn(event: Event) -> Status,
    pub check_event: *const c_void,
    pub install_protocol_interface: *const c_void,
    pub reinstall_protocol_interface: *const c_void,
    pub uninstall_protocol_interface: *const c_void,
    pub handle_protocol: unsafe extern "efiapi" fn(
        handle: Handle,
        protocol: *const Guid,
        interface: *mut *mut c_void,
    ) -> Status,
    pub reserved: *const c_void,
    pub register_protocol_notify: *const c_void,
    pub locate_handle: *const c_void,
    pub locate_device_path: *const c_void,
    pub install_configuration_table: *const c_void,
    pub load_image: unsafe extern "efiapi" fn(
        boot_policy: u8,
        parent_image_handle: Handle,
        device_path: *const DevicePathHeader,
        source_buffer: *const u8,
        source_size: usize,
        image_handle: *mut Handle,
    ) -> Status,
    pub start_image: unsafe extern "efiapi" fn(
        image_handle: Handle,
        exit_data_size: *mut usize,
        exit_data: *mut *mut u16,
    ) -> Status,
    pub exit: *const c_void,
    pub unload_image: *const c_void,
    pub exit_boot_services:
        unsafe extern "efiapi" fn(image_handle: Handle, map_key: usize) -> Status,
    pub get_next_monotonic_count: *const c_void,
    pub stall: unsafe extern "efiapi" fn(microseconds: usize) -> Status,
    pub set_watchdog_timer: *const c_void,
    pub connect_controller: *const c_void,
    pub disconnect_controller: *const c_void,
    pub open_protocol: *const c_void,
    pub close_protocol: *const c_void,
    pub open_protocol_information: *const c_void,
    pub protocols_per_handle: *const c_void,
    pub locate_handle_buffer: unsafe extern "efiapi" fn(
        search_type: u32,
        protocol: *const Guid,
        search_key: *const c_void,
        no_handles: *mut usize,
        buffer: *mut *mut Handle,
    ) -> Status,
    pub locate_protocol: *const c_void,
    pub install_multiple_protocol_interfaces: *const c_void,
    pub uninstall_multiple_protocol_interfaces: *const c_void,
    pub calculate_crc32: *const c_void,
    pub copy_mem: *const c_void,
    pub set_mem: *const c_void,
    pub create_event_ex: *const c_void,
}

#[repr(C)]
pub struct SimpleTextOutputProtocol {
    pub reset: *const c_void,
    pub output_string: unsafe extern "efiapi" fn(*mut Self, *const u16) -> Status,
    pub test_string: *const c_void,
    pub query_mode: *const c_void,
    pub set_mode: *const c_void,
    pub set_attribute: *const c_void,
    pub clear_screen: *const c_void,
    pub set_cursor_position: *const c_void,
    pub enable_cursor: *const c_void,
    pub mode: *const c_void,
}

#[repr(C)]
pub struct SystemTable {
    pub hdr: TableHeader,
    pub firmware_vendor: *const u16,
    pub firmware_revision: u32,
    pub console_in_handle: Handle,
    pub con_in: *mut c_void,
    pub console_out_handle: Handle,
    pub con_out: *mut SimpleTextOutputProtocol,
    pub standard_error_handle: Handle,
    pub std_err: *mut SimpleTextOutputProtocol,
    pub runtime_services: *mut RuntimeServices,
    pub boot_services: *mut BootServices,
    pub number_of_table_entries: usize,
    pub configuration_table: *const c_void,
}

#[repr(C)]
pub struct LoadedImageProtocol {
    pub revision: u32,
    pub parent_handle: Handle,
    pub system_table: *mut SystemTable,
    pub device_handle: Handle,
    pub file_path: *const DevicePathHeader,
    pub reserved: *const c_void,
    pub load_options_size: u32,
    pub load_options: *const c_void,
    pub image_base: *const c_void,
    pub image_size: u64,
    pub image_code_type: u32,
    pub image_data_type: u32,
    pub unload: *const c_void,
}

#[repr(C)]
pub struct SimpleFileSystemProtocol {
    pub revision: u64,
    pub open_volume: unsafe extern "efiapi" fn(*mut Self, *mut *mut FileProtocol) -> Status,
}

#[repr(C)]
pub struct FileProtocol {
    pub revision: u64,
    pub open: unsafe extern "efiapi" fn(
        *mut Self,
        *mut *mut FileProtocol,
        *const u16,
        u64,
        u64,
    ) -> Status,
    pub close: unsafe extern "efiapi" fn(*mut Self) -> Status,
    pub delete: *const c_void,
    pub read: unsafe extern "efiapi" fn(*mut Self, *mut usize, *mut u8) -> Status,
    pub write: *const c_void,
    pub get_position: *const c_void,
    pub set_position: *const c_void,
    pub get_info: *const c_void,
    pub set_info: *const c_void,
    pub flush: *const c_void,
}

#[repr(C)]
#[derive(Copy, Clone, Debug)]
pub struct DevicePathHeader {
    pub r#type: u8,
    pub sub_type: u8,
    pub length: [u8; 2],
}

// Verify x86_64 UEFI ABI offsets at compile time so field drift fails the build.
const _: () = {
    assert!(size_of::<TableHeader>() == 24);
    assert!(offset_of!(RuntimeServices, get_variable) == 72);
    assert!(offset_of!(RuntimeServices, set_variable) == 88);
    assert!(offset_of!(BootServices, allocate_pool) == 64);
    assert!(offset_of!(BootServices, free_pool) == 72);
    assert!(offset_of!(BootServices, create_event) == 80);
    assert!(offset_of!(BootServices, close_event) == 112);
    assert!(offset_of!(BootServices, handle_protocol) == 152);
    assert!(offset_of!(BootServices, load_image) == 200);
    assert!(offset_of!(BootServices, start_image) == 208);
    assert!(offset_of!(BootServices, exit_boot_services) == 232);
    assert!(offset_of!(BootServices, stall) == 248);
    assert!(offset_of!(BootServices, locate_handle_buffer) == 312);
    assert!(offset_of!(BootServices, calculate_crc32) == 344);
    assert!(offset_of!(FileProtocol, read) == 32);
};

/// Writes an ASCII string to UEFI `ConOut`, expanding `\n` to `\r\n`.
///
/// # Safety
/// `st` must be a valid `SystemTable` pointer while boot services are active.
pub unsafe fn write_console(st: *mut SystemTable, msg: &str) {
    if st.is_null() {
        return;
    }
    let con_out = unsafe { (*st).con_out };
    if con_out.is_null() {
        return;
    }

    let mut buf = [0u16; 128];
    let mut len = 0usize;

    for &b in msg.as_bytes() {
        if b == b'\n' {
            if len + 2 >= buf.len() {
                buf[len] = 0;
                unsafe {
                    ((*con_out).output_string)(con_out, buf.as_ptr());
                }
                len = 0;
            }
            buf[len] = u16::from(b'\r');
            buf[len + 1] = u16::from(b'\n');
            len += 2;
        } else {
            if len + 1 >= buf.len() {
                buf[len] = 0;
                unsafe {
                    ((*con_out).output_string)(con_out, buf.as_ptr());
                }
                len = 0;
            }
            let ch = if b.is_ascii() { b } else { b'?' };
            buf[len] = u16::from(ch);
            len += 1;
        }
    }

    if len > 0 {
        buf[len] = 0;
        unsafe {
            ((*con_out).output_string)(con_out, buf.as_ptr());
        }
    }
}

/// Writes a 64-bit unsigned integer to UEFI `ConOut` as `0x` followed by 16 hex digits.
///
/// # Safety
/// `st` must be a valid `SystemTable` pointer while boot services are active.
pub unsafe fn write_hex_u64(st: *mut SystemTable, val: u64) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut bytes = [0u8; 18];
    bytes[0] = b'0';
    bytes[1] = b'x';
    let mut i = 0usize;
    while i < 16 {
        let shift = (15 - i) * 4;
        let nibble = ((val >> shift) & 0xF) as usize;
        bytes[2 + i] = HEX[nibble];
        i += 1;
    }
    if let Ok(s) = core::str::from_utf8(&bytes) {
        unsafe { write_console(st, s) };
    }
}

/// Writes a label and hexadecimal UEFI `Status` value followed by a newline.
///
/// # Safety
/// `st` must be a valid `SystemTable` pointer while boot services are active.
pub unsafe fn write_status(st: *mut SystemTable, prefix: &str, status: Status) {
    unsafe {
        write_console(st, prefix);
        write_hex_u64(st, status.0 as u64);
        write_console(st, "\n");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_offsets_match_uefi_spec() {
        assert_eq!(size_of::<TableHeader>(), 24);
        assert_eq!(offset_of!(RuntimeServices, get_variable), 72);
        assert_eq!(offset_of!(RuntimeServices, set_variable), 88);
        assert_eq!(offset_of!(BootServices, allocate_pool), 64);
        assert_eq!(offset_of!(BootServices, free_pool), 72);
        assert_eq!(offset_of!(BootServices, create_event), 80);
        assert_eq!(offset_of!(BootServices, close_event), 112);
        assert_eq!(offset_of!(BootServices, handle_protocol), 152);
        assert_eq!(offset_of!(BootServices, load_image), 200);
        assert_eq!(offset_of!(BootServices, start_image), 208);
        assert_eq!(offset_of!(BootServices, exit_boot_services), 232);
        assert_eq!(offset_of!(BootServices, stall), 248);
        assert_eq!(offset_of!(BootServices, locate_handle_buffer), 312);
        assert_eq!(offset_of!(BootServices, calculate_crc32), 344);
        assert_eq!(offset_of!(FileProtocol, read), 32);
    }

    #[test]
    fn guid_equality_works() {
        let copy = CRDYBOOT_VAR_VENDOR;
        assert_eq!(copy, CRDYBOOT_VAR_VENDOR);
        assert_ne!(CRDYBOOT_VAR_VENDOR, SBAT_VAR_VENDOR);
        assert_ne!(SBAT_VAR_VENDOR, FWUPDATE_VENDOR);
    }
}
