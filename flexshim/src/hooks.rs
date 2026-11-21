// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

use crate::config::{Config, MAX_CMDLINE_UNITS, build_merged_cmdline};
use crate::crc32::update_table_crc32;
use crate::shadow::{
    ShadowStore, is_crdyboot_log_var, is_intercepted_vendor, is_shadow_only_vendor,
};
use crate::uefi::{
    BootServices, EVT_SIGNAL_EXIT_BOOT_SERVICES, Event, GetVariableFn, Guid, Handle,
    LOADED_IMAGE_PROTOCOL_GUID, LoadedImageProtocol, RuntimeServices, SetVariableFn, Status,
    SystemTable, TPL_NOTIFY, write_console,
};
use core::cell::UnsafeCell;
use core::ffi::c_void;
use core::ptr;
use core::slice;
use core::sync::atomic::{AtomicBool, Ordering};

const MAX_VAR_NAME_UNITS: usize = 512;

struct HookState {
    system_table: *mut SystemTable,
    boot_services: *mut BootServices,
    runtime_services: *mut RuntimeServices,
    orig_get_variable: Option<GetVariableFn>,
    orig_set_variable: Option<SetVariableFn>,
    exit_bs_event: Event,
    child_handle: Handle,
    config: Config,
    cmdline_injected: bool,
    cmdline_buf: [u16; MAX_CMDLINE_UNITS],
    shadow: ShadowStore,
}

impl HookState {
    const fn new() -> Self {
        Self {
            system_table: ptr::null_mut(),
            boot_services: ptr::null_mut(),
            runtime_services: ptr::null_mut(),
            orig_get_variable: None,
            orig_set_variable: None,
            exit_bs_event: ptr::null_mut(),
            child_handle: ptr::null_mut(),
            config: Config::new(),
            cmdline_injected: false,
            cmdline_buf: [0; MAX_CMDLINE_UNITS],
            shadow: ShadowStore::new(),
        }
    }
}

struct SyncHookState(UnsafeCell<HookState>);

// UEFI boot services execute single-threaded on the BSP before ExitBootServices.
unsafe impl Sync for SyncHookState {}

static STATE: SyncHookState = SyncHookState(UnsafeCell::new(HookState::new()));
static RESTORED: AtomicBool = AtomicBool::new(false);

unsafe fn read_cstr16<'a>(ptr: *const u16, max_units: usize) -> Option<&'a [u16]> {
    if ptr.is_null() {
        return None;
    }
    let mut len = 0usize;
    while len < max_units {
        let unit = unsafe { *ptr.add(len) };
        if unit == 0 {
            return Some(unsafe { slice::from_raw_parts(ptr, len) });
        }
        len += 1;
    }
    None
}

unsafe fn maybe_inject_load_options(state: &mut HookState) {
    if state.cmdline_injected
        || !state.config.has_cmdline_additions()
        || state.child_handle.is_null()
        || state.boot_services.is_null()
    {
        return;
    }
    state.cmdline_injected = true;

    let mut li_ptr: *mut c_void = ptr::null_mut();
    let status = unsafe {
        ((*state.boot_services).handle_protocol)(
            state.child_handle,
            &LOADED_IMAGE_PROTOCOL_GUID,
            &raw mut li_ptr,
        )
    };
    if status.is_error() || li_ptr.is_null() {
        unsafe {
            write_console(
                state.system_table,
                "flexshim: failed to open LoadedImage for boot options\n",
            );
        }
        return;
    }

    let li = li_ptr.cast::<LoadedImageProtocol>();
    let existing_ptr = unsafe { (*li).load_options };
    let existing_bytes = unsafe { usize::try_from((*li).load_options_size).unwrap_or(0) };
    let existing_slice: &[u16] =
        if existing_ptr.is_null() || existing_bytes < 2 || !(existing_ptr as usize).is_multiple_of(2)
        {
            &[]
        } else {
            unsafe { slice::from_raw_parts(existing_ptr.cast::<u16>(), existing_bytes / 2) }
        };

    match build_merged_cmdline(existing_slice, &state.config, &mut state.cmdline_buf) {
        Some(new_units) => unsafe {
            (*li).load_options = state.cmdline_buf.as_ptr().cast();
            (*li).load_options_size = u32::try_from(new_units * 2).unwrap_or(u32::MAX);
            write_console(
                state.system_table,
                "flexshim: applied boot options from flexshim.cfg\n",
            );
        },
        None => unsafe {
            write_console(
                state.system_table,
                "flexshim: boot options too long; leaving kernel cmdline unchanged\n",
            );
        },
    }
}

unsafe extern "efiapi" fn hooked_set_variable(
    variable_name: *const u16,
    vendor_guid: *const Guid,
    attributes: u32,
    data_size: usize,
    data: *const u8,
) -> Status {
    if variable_name.is_null() || vendor_guid.is_null() {
        return Status::INVALID_PARAMETER;
    }
    if data_size > 0 && data.is_null() {
        return Status::INVALID_PARAMETER;
    }

    let Some(name) = (unsafe { read_cstr16(variable_name, MAX_VAR_NAME_UNITS) }) else {
        return Status::INVALID_PARAMETER;
    };
    let vendor = unsafe { &*vendor_guid };

    if is_intercepted_vendor(vendor) {
        let data_slice = if data_size == 0 {
            &[]
        } else {
            unsafe { slice::from_raw_parts(data, data_size) }
        };
        let state = unsafe { &mut *STATE.0.get() };
        // crdyboot sets CrdybootLog right after populating LoadOptions and before jumping to the kernel.
        if is_crdyboot_log_var(name, vendor) {
            unsafe { maybe_inject_load_options(state) };
        }
        return state.shadow.set(name, vendor, attributes, data_slice);
    }

    let orig = unsafe { (*STATE.0.get()).orig_set_variable };
    match orig {
        Some(f) => unsafe { f(variable_name, vendor_guid, attributes, data_size, data) },
        None => Status::UNSUPPORTED,
    }
}

unsafe extern "efiapi" fn hooked_get_variable(
    variable_name: *const u16,
    vendor_guid: *const Guid,
    attributes: *mut u32,
    data_size: *mut usize,
    data: *mut u8,
) -> Status {
    if variable_name.is_null() || vendor_guid.is_null() || data_size.is_null() {
        return Status::INVALID_PARAMETER;
    }
    let caller_size = unsafe { *data_size };
    if caller_size > 0 && data.is_null() {
        return Status::INVALID_PARAMETER;
    }

    let Some(name) = (unsafe { read_cstr16(variable_name, MAX_VAR_NAME_UNITS) }) else {
        return Status::INVALID_PARAMETER;
    };
    let vendor = unsafe { &*vendor_guid };

    if is_intercepted_vendor(vendor) {
        let out_attrs = if attributes.is_null() {
            None
        } else {
            Some(unsafe { &mut *attributes })
        };
        let out_buf = if data.is_null() || caller_size == 0 {
            None
        } else {
            Some(unsafe { slice::from_raw_parts_mut(data, caller_size) })
        };
        let mut size_val = caller_size;
        let status = {
            let state = unsafe { &*STATE.0.get() };
            state.shadow.get(name, vendor, out_attrs, out_buf, &mut size_val)
        };
        if status != Status::NOT_FOUND || is_shadow_only_vendor(vendor) {
            unsafe { *data_size = size_val };
            return status;
        }
    }

    let orig = unsafe { (*STATE.0.get()).orig_get_variable };
    match orig {
        Some(f) => unsafe { f(variable_name, vendor_guid, attributes, data_size, data) },
        None => Status::UNSUPPORTED,
    }
}

unsafe extern "efiapi" fn on_exit_boot_services(_event: Event, _context: *mut c_void) {
    unsafe { restore_hooks() };
}

/// Restores original `GetVariable` and `SetVariable` pointers and table CRC32.
///
/// Safe to invoke multiple times and inside `EVT_SIGNAL_EXIT_BOOT_SERVICES`
/// because it touches only static state and the runtime services header.
///
/// # Safety
/// `STATE.runtime_services` must still point to the active `RuntimeServices` table.
pub unsafe fn restore_hooks() {
    if RESTORED.swap(true, Ordering::AcqRel) {
        return;
    }
    let state = unsafe { &mut *STATE.0.get() };
    let rt = state.runtime_services;
    if !rt.is_null() {
        if let Some(orig_get) = state.orig_get_variable {
            unsafe { (*rt).get_variable = orig_get };
        }
        if let Some(orig_set) = state.orig_set_variable {
            unsafe { (*rt).set_variable = orig_set };
        }
        unsafe { update_table_crc32(&raw mut (*rt).hdr) };
    }
}

/// Records the handle of the chainloaded `crdyboot` image for `LoadOptions` injection.
///
/// # Safety
/// `handle` must be null or a valid `LoadedImage` handle created by `LoadImage`.
pub unsafe fn set_child_image_handle(handle: Handle) {
    let state = unsafe { &mut *STATE.0.get() };
    state.child_handle = handle;
}

/// Installs variable hooks on `SystemTable->RuntimeServices` and registers an
/// `ExitBootServices` restoration event.
///
/// # Safety
/// `st` must point to a valid UEFI `SystemTable` with active boot services.
pub unsafe fn install_hooks(st: *mut SystemTable, config: Config) -> Result<(), Status> {
    if st.is_null() {
        return Err(Status::INVALID_PARAMETER);
    }
    let rt = unsafe { (*st).runtime_services };
    let bs = unsafe { (*st).boot_services };
    if rt.is_null() || bs.is_null() {
        return Err(Status::INVALID_PARAMETER);
    }

    let mut event: Event = ptr::null_mut();
    let status = unsafe {
        ((*bs).create_event)(
            EVT_SIGNAL_EXIT_BOOT_SERVICES,
            TPL_NOTIFY,
            Some(on_exit_boot_services),
            ptr::null_mut(),
            &raw mut event,
        )
    };
    if status.is_error() {
        return Err(status);
    }

    let state = unsafe { &mut *STATE.0.get() };
    state.system_table = st;
    state.boot_services = bs;
    state.runtime_services = rt;
    state.orig_get_variable = Some(unsafe { (*rt).get_variable });
    state.orig_set_variable = Some(unsafe { (*rt).set_variable });
    state.exit_bs_event = event;
    state.child_handle = ptr::null_mut();
    state.config = config;
    state.cmdline_injected = false;
    state.shadow = ShadowStore::new();
    RESTORED.store(false, Ordering::Release);

    unsafe {
        (*rt).get_variable = hooked_get_variable;
        (*rt).set_variable = hooked_set_variable;
        update_table_crc32(&raw mut (*rt).hdr);
    }
    Ok(())
}

/// Restores runtime service pointers and closes the `ExitBootServices` event if
/// a chainloaded image returns control without exiting boot services.
///
/// # Safety
/// `bs` must be null or point to active UEFI `BootServices`.
pub unsafe fn cleanup_on_return(bs: *mut BootServices) {
    unsafe { restore_hooks() };
    let state = unsafe { &mut *STATE.0.get() };
    if !bs.is_null() && !state.exit_bs_event.is_null() {
        unsafe {
            ((*bs).close_event)(state.exit_bs_event);
        }
        state.exit_bs_event = ptr::null_mut();
    }
}
