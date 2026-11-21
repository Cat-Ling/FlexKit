// SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
// SPDX-License-Identifier: GPL-3.0-or-later

#![cfg_attr(target_os = "uefi", no_std)]
#![cfg_attr(target_os = "uefi", no_main)]

#[cfg(target_os = "uefi")]
mod efi_entry {
    use core::panic::PanicInfo;
    use flexshim::hooks::{cleanup_on_return, install_hooks};
    use flexshim::loader::{chainload_crdyboot, load_config};
    use flexshim::uefi::{Handle, Status, SystemTable, write_console, write_status};

    const ERROR_STALL_US: usize = 5_000_000;

    #[unsafe(no_mangle)]
    pub extern "efiapi" fn efi_main(image_handle: Handle, system_table: *mut SystemTable) -> Status {
        if system_table.is_null() {
            return Status::INVALID_PARAMETER;
        }
        let bs = unsafe { (*system_table).boot_services };

        unsafe {
            write_console(
                system_table,
                "flexshim: installing UEFI variable compatibility shim\n",
            );
        }

        let cfg = unsafe { load_config(image_handle, system_table) };
        if cfg.block_updates {
            unsafe {
                write_console(
                    system_table,
                    "flexshim: config block_updates=true (appending cros_debug)\n",
                );
            }
        }
        if !cfg.extra_cmdline().is_empty() {
            unsafe {
                write_console(system_table, "flexshim: config extra_cmdline enabled\n");
            }
        }

        if let Err(status) = unsafe { install_hooks(system_table, cfg) } {
            unsafe {
                write_status(system_table, "flexshim: hook installation failed: ", status);
                if !bs.is_null() {
                    ((*bs).stall)(ERROR_STALL_US);
                }
            }
            return status;
        }

        unsafe {
            write_console(
                system_table,
                "flexshim: searching for \\efi\\boot\\crdybootx64.efi\n",
            );
        }

        match unsafe { chainload_crdyboot(image_handle, system_table) } {
            Ok(()) => {
                unsafe { cleanup_on_return(bs) };
                Status::SUCCESS
            }
            Err(err) => {
                unsafe {
                    cleanup_on_return(bs);
                    write_console(system_table, "flexshim: ");
                    write_console(system_table, err.stage);
                    write_status(system_table, " failed: ", err.status);
                    if !bs.is_null() {
                        ((*bs).stall)(ERROR_STALL_US);
                    }
                }
                err.status
            }
        }
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
