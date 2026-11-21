// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

#![cfg_attr(target_os = "uefi", no_std)]
#![cfg_attr(target_os = "uefi", no_main)]

#[cfg(target_os = "uefi")]
mod test_efi {
    use core::arch::asm;
    use core::cell::UnsafeCell;
    use core::ffi::c_void;
    use core::panic::PanicInfo;
    use core::ptr;
    use core::slice;
    use core::sync::atomic::{AtomicBool, Ordering};
    use flexshim::crc32::{crc32, update_table_crc32};
    use flexshim::devpath::{
        self, DEVPATH_SUBTYPE_END_ENTIRE, DEVPATH_TYPE_END,
    };
    use flexshim::uefi::{
        BootServices, CRDYBOOT_VAR_VENDOR, DEVICE_PATH_PROTOCOL_GUID, DevicePathHeader,
        EVT_SIGNAL_EXIT_BOOT_SERVICES, Event, Guid, Handle, LOADED_IMAGE_PROTOCOL_GUID,
        LoadedImageProtocol, MEMORY_TYPE_LOADER_DATA, RuntimeServices, SBAT_VAR_VENDOR,
        SetVariableFn, Status, SystemTable, TPL_CALLBACK, write_console, write_status,
    };

    const MAX_DP_BYTES: usize = 16 * 1024;
    const COM1_DATA: u16 = 0x3F8;
    const COM1_LSR: u16 = 0x3FD;

    // `\efi\boot\nextx64.efi` + NUL
    const NEXT_PATH_UTF16: [u16; 22] = [
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
        b'n' as u16,
        b'e' as u16,
        b'x' as u16,
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

    type ExitBootServicesFn = unsafe extern "efiapi" fn(Handle, usize) -> Status;

    struct InjectorState {
        rt: *mut RuntimeServices,
        bs: *mut BootServices,
        orig_set_variable: Option<SetVariableFn>,
        orig_exit_boot_services: Option<ExitBootServicesFn>,
    }

    struct SyncState(UnsafeCell<InjectorState>);
    unsafe impl Sync for SyncState {}

    static STATE: SyncState = SyncState(UnsafeCell::new(InjectorState {
        rt: ptr::null_mut(),
        bs: ptr::null_mut(),
        orig_set_variable: None,
        orig_exit_boot_services: None,
    }));

    static DUMPED_CMDLINE: AtomicBool = AtomicBool::new(false);

    #[inline]
    unsafe fn com1_write_byte(b: u8) {
        let mut spins = 0u32;
        while spins < 100_000 {
            let lsr: u8;
            unsafe {
                asm!("in al, dx", in("dx") COM1_LSR, out("al") lsr, options(nomem, nostack));
            }
            if (lsr & 0x20) != 0 {
                break;
            }
            spins += 1;
        }
        unsafe {
            asm!("out dx, al", in("dx") COM1_DATA, in("al") b, options(nomem, nostack));
        }
    }

    unsafe fn com1_write_bytes(bytes: &[u8]) {
        for &b in bytes {
            unsafe { com1_write_byte(b) };
        }
    }

    unsafe extern "efiapi" fn faulty_set_variable(
        variable_name: *const u16,
        vendor_guid: *const Guid,
        attributes: u32,
        data_size: usize,
        data: *const u8,
    ) -> Status {
        if !vendor_guid.is_null() {
            let vendor = unsafe { &*vendor_guid };
            if *vendor == CRDYBOOT_VAR_VENDOR || *vendor == SBAT_VAR_VENDOR {
                return Status::INVALID_PARAMETER;
            }
        }
        let orig = unsafe { (*STATE.0.get()).orig_set_variable };
        match orig {
            Some(f) => unsafe { f(variable_name, vendor_guid, attributes, data_size, data) },
            None => Status::UNSUPPORTED,
        }
    }

    unsafe extern "efiapi" fn hooked_exit_boot_services(
        image_handle: Handle,
        map_key: usize,
    ) -> Status {
        let state = unsafe { &mut *STATE.0.get() };
        let bs = state.bs;

        if !DUMPED_CMDLINE.swap(true, Ordering::AcqRel) && !bs.is_null() {
            let mut li_ptr: *mut c_void = ptr::null_mut();
            let status = unsafe {
                ((*bs).handle_protocol)(
                    image_handle,
                    &LOADED_IMAGE_PROTOCOL_GUID,
                    &raw mut li_ptr,
                )
            };
            if status.is_success() && !li_ptr.is_null() {
                let li = li_ptr.cast::<LoadedImageProtocol>();
                let ptr = unsafe { (*li).load_options };
                let size = unsafe { usize::try_from((*li).load_options_size).unwrap_or(0) };
                if !ptr.is_null() && size >= 2 && (ptr as usize).is_multiple_of(2) {
                    let units = unsafe { slice::from_raw_parts(ptr.cast::<u16>(), size / 2) };
                    unsafe { com1_write_bytes(b"FAULT_INJ: KERNEL_LOAD_OPTIONS=") };
                    for &unit in units {
                        if unit == 0 {
                            break;
                        }
                        let ch = if (0x20..=0x7E).contains(&unit) {
                            unit as u8
                        } else {
                            b'?'
                        };
                        unsafe { com1_write_byte(ch) };
                    }
                    unsafe { com1_write_bytes(b"\r\n") };
                }
            }
        }

        if let Some(orig_ebs) = state.orig_exit_boot_services {
            if !bs.is_null() {
                unsafe {
                    (*bs).exit_boot_services = orig_ebs;
                    update_table_crc32(&raw mut (*bs).hdr);
                }
            }
            unsafe { orig_ebs(image_handle, map_key) }
        } else {
            Status::UNSUPPORTED
        }
    }

    unsafe extern "efiapi" fn on_exit_boot_services(_event: Event, _ctx: *mut c_void) {
        let rt = unsafe { (*STATE.0.get()).rt };
        if rt.is_null() {
            unsafe { com1_write_bytes(b"FAULT_INJ: RT_RESTORE_FAIL (null rt)\r\n") };
            return;
        }

        let ptr_ok = unsafe {
            ((*rt).set_variable as *const () as usize) == (faulty_set_variable as *const () as usize)
        };

        let recorded = unsafe { (*rt).hdr.crc32 };
        let hdr_size = unsafe { (*rt).hdr.header_size as usize };
        let crc_ok = if hdr_size >= core::mem::size_of::<flexshim::uefi::TableHeader>() {
            unsafe {
                (*rt).hdr.crc32 = 0;
                let computed = crc32(slice::from_raw_parts(rt.cast::<u8>(), hdr_size));
                (*rt).hdr.crc32 = recorded;
                computed == recorded
            }
        } else {
            false
        };

        if ptr_ok && crc_ok {
            unsafe { com1_write_bytes(b"FAULT_INJ: RT_RESTORE_OK\r\n") };
        } else if !ptr_ok && !crc_ok {
            unsafe { com1_write_bytes(b"FAULT_INJ: RT_RESTORE_FAIL (ptr+crc)\r\n") };
        } else if !ptr_ok {
            unsafe { com1_write_bytes(b"FAULT_INJ: RT_RESTORE_FAIL (ptr)\r\n") };
        } else {
            unsafe { com1_write_bytes(b"FAULT_INJ: RT_RESTORE_FAIL (crc)\r\n") };
        }
    }

    unsafe fn build_next_device_path(
        bs: *mut BootServices,
        device_handle: Handle,
    ) -> Result<*mut DevicePathHeader, Status> {
        let mut dp_ptr: *mut c_void = ptr::null_mut();
        let status = unsafe {
            ((*bs).handle_protocol)(
                device_handle,
                &DEVICE_PATH_PROTOCOL_GUID,
                &raw mut dp_ptr,
            )
        };
        if status.is_error() || dp_ptr.is_null() {
            return Err(status);
        }

        let base = dp_ptr.cast::<u8>();
        let mut offset = 0usize;
        let mut raw_len = None;
        while offset + 4 <= MAX_DP_BYTES {
            let hdr = unsafe { slice::from_raw_parts(base.add(offset), 4) };
            let node_len = usize::from(u16::from_le_bytes([hdr[2], hdr[3]]));
            if node_len < 4 || offset + node_len > MAX_DP_BYTES {
                break;
            }
            offset += node_len;
            if hdr[0] == DEVPATH_TYPE_END && hdr[1] == DEVPATH_SUBTYPE_END_ENTIRE {
                raw_len = Some(offset);
                break;
            }
        }
        let raw_len = raw_len.ok_or(Status::INVALID_PARAMETER)?;
        let raw_slice = unsafe { slice::from_raw_parts(base, raw_len) };
        let plen = devpath::prefix_len(raw_slice).ok_or(Status::INVALID_PARAMETER)?;
        let total =
            devpath::total_file_dp_len(plen, &NEXT_PATH_UTF16).ok_or(Status::INVALID_PARAMETER)?;

        let mut buf: *mut u8 = ptr::null_mut();
        let alloc = unsafe { ((*bs).allocate_pool)(MEMORY_TYPE_LOADER_DATA, total, &raw mut buf) };
        if alloc.is_error() || buf.is_null() {
            return Err(alloc);
        }
        let out = unsafe { slice::from_raw_parts_mut(buf, total) };
        if devpath::write_file_device_path(&raw_slice[..plen], &NEXT_PATH_UTF16, out).is_none() {
            unsafe { ((*bs).free_pool)(buf) };
            return Err(Status::INVALID_PARAMETER);
        }
        Ok(buf.cast())
    }

    #[unsafe(no_mangle)]
    pub extern "efiapi" fn efi_main(image_handle: Handle, st: *mut SystemTable) -> Status {
        if st.is_null() {
            return Status::INVALID_PARAMETER;
        }
        let rt = unsafe { (*st).runtime_services };
        let bs = unsafe { (*st).boot_services };
        if rt.is_null() || bs.is_null() {
            return Status::INVALID_PARAMETER;
        }

        let state = unsafe { &mut *STATE.0.get() };
        state.rt = rt;
        state.bs = bs;
        state.orig_set_variable = Some(unsafe { (*rt).set_variable });
        state.orig_exit_boot_services = Some(unsafe { (*bs).exit_boot_services });
        DUMPED_CMDLINE.store(false, Ordering::Release);

        let mut event: Event = ptr::null_mut();
        let ev_status = unsafe {
            ((*bs).create_event)(
                EVT_SIGNAL_EXIT_BOOT_SERVICES,
                TPL_CALLBACK,
                Some(on_exit_boot_services),
                ptr::null_mut(),
                &raw mut event,
            )
        };
        if ev_status.is_error() {
            unsafe { write_status(st, "FAULT_INJ: CreateEvent failed: ", ev_status) };
            return ev_status;
        }

        unsafe {
            (*rt).set_variable = faulty_set_variable;
            update_table_crc32(&raw mut (*rt).hdr);
            (*bs).exit_boot_services = hooked_exit_boot_services;
            update_table_crc32(&raw mut (*bs).hdr);
            write_console(
                st,
                "FAULT_INJ: SetVariable hooked, chainloading \\efi\\boot\\nextx64.efi\n",
            );
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
            unsafe { write_status(st, "FAULT_INJ: LoadedImage failed: ", li_status) };
            return li_status;
        }
        let device_handle = unsafe { (*li_ptr.cast::<LoadedImageProtocol>()).device_handle };

        let dp = match unsafe { build_next_device_path(bs, device_handle) } {
            Ok(dp) => dp,
            Err(status) => {
                unsafe { write_status(st, "FAULT_INJ: DevicePath failed: ", status) };
                return status;
            }
        };

        let mut child: Handle = ptr::null_mut();
        let load_status =
            unsafe { ((*bs).load_image)(0, image_handle, dp, ptr::null(), 0, &raw mut child) };
        unsafe { ((*bs).free_pool)(dp.cast()) };
        if load_status.is_error() {
            unsafe { write_status(st, "FAULT_INJ: LoadImage failed: ", load_status) };
            return load_status;
        }

        let mut exit_sz = 0usize;
        let mut exit_data: *mut u16 = ptr::null_mut();
        let start_status =
            unsafe { ((*bs).start_image)(child, &raw mut exit_sz, &raw mut exit_data) };
        if !exit_data.is_null() {
            unsafe { ((*bs).free_pool)(exit_data.cast()) };
        }
        if start_status.is_error() {
            unsafe { write_status(st, "FAULT_INJ: StartImage failed: ", start_status) };
        }
        start_status
    }

    #[panic_handler]
    fn panic(_info: &PanicInfo) -> ! {
        loop {
            core::hint::spin_loop();
        }
    }
}

#[cfg(not(target_os = "uefi"))]
fn main() {}
