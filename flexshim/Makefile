# SPDX-FileCopyrightText: 2026 Cat-Ling <cat-ling@yemoja.xyz>
# SPDX-License-Identifier: GPL-3.0-or-later

CARGO ?= cargo
TARGET ?= x86_64-unknown-uefi
TARGET_BIN = target/$(TARGET)/release/flexshimx64.efi
OUT_DIR = build
OUT_BIN = $(OUT_DIR)/flexshimx64.efi
OUT_CFG = $(OUT_DIR)/flexshim.cfg

.PHONY: all test verify-qemu clean

all: $(OUT_BIN) $(OUT_CFG)

$(OUT_BIN): src/lib.rs src/main.rs src/uefi.rs src/crc32.rs src/shadow.rs src/devpath.rs src/hooks.rs src/loader.rs src/config.rs Cargo.toml
	$(CARGO) build --release --target $(TARGET) --bin flexshimx64
	mkdir -p $(OUT_DIR)
	cp $(TARGET_BIN) $(OUT_BIN)

$(OUT_CFG): flexshim.cfg
	mkdir -p $(OUT_DIR)
	cp flexshim.cfg $(OUT_CFG)

test:
	$(CARGO) test --lib

verify-qemu: all
	./tests/qemu_verify.sh

clean:
	$(CARGO) clean
	$(CARGO) clean --manifest-path tests/fault_injector/Cargo.toml
	rm -rf $(OUT_DIR)
