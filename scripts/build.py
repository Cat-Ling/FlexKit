#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flexkit import APP_ID, APP_NAME, APP_SLUG, __version__  # noqa: E402


def _render_icon_pngs() -> dict[int, bytes]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from flexkit.ui.qt import QtCore, QtGui, QtWidgets
    from flexkit.ui.theme import render_app_icon

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    icon = render_app_icon()
    pngs: dict[int, bytes] = {}
    for px in (16, 32, 48, 64, 128, 256, 512):
        pm = icon.pixmap(px, px)
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        pm.save(buf, "PNG")
        buf.close()
        pngs[px] = bytes(ba.data())
    _ = QtGui
    return pngs


def _write_ico(dest: Path, pngs: dict[int, bytes]) -> None:
    sizes = [16, 32, 48, 64, 128, 256]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries: list[bytes] = []
    blobs: list[bytes] = []
    for sz in sizes:
        data = pngs[sz]
        dim = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    dest.write_bytes(header + b"".join(entries) + b"".join(blobs))


def _write_icns(dest: Path, pngs: dict[int, bytes]) -> None:
    ostypes = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09"}
    chunks: list[bytes] = []
    for sz, tag in ostypes.items():
        data = pngs[sz]
        chunks.append(tag + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(chunks)
    dest.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def ensure_platform_icons(build_dir: Path) -> dict[str, Path]:
    build_dir.mkdir(parents=True, exist_ok=True)
    pngs = _render_icon_pngs()
    png_path = build_dir / "app.png"
    ico_path = build_dir / "app.ico"
    icns_path = build_dir / "app.icns"
    png_path.write_bytes(pngs[256])
    _write_ico(ico_path, pngs)
    _write_icns(icns_path, pngs)
    return {"png": png_path, "ico": ico_path, "icns": icns_path}


def _norm_arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "unknown"


def _norm_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def _write_sha256(artifact: Path) -> Path:
    h = hashlib.sha256()
    with open(artifact, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    sha_file = artifact.with_name(f"{artifact.name}.sha256")
    sha_file.write_text(f"{h.hexdigest()}  {artifact.name}\n", encoding="utf-8")
    return sha_file


def _supports_symlinks(directory: Path) -> bool:
    if sys.platform == "win32":
        return True
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".symlink_probe_{os.getpid()}"
    try:
        probe.symlink_to(".")
        probe.unlink()
        return True
    except OSError:
        probe.unlink(missing_ok=True)
        return False


def _find_cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo:
        return cargo
    exe_name = "cargo.exe" if sys.platform == "win32" else "cargo"
    home_cargo = Path.home() / ".cargo" / "bin" / exe_name
    if home_cargo.is_file():
        return str(home_cargo)
    raise RuntimeError("Rust cargo toolchain not found on PATH or ~/.cargo/bin")


def build_flexshim(assets_flexshim_dir: Path, *, skip_build: bool = False) -> tuple[Path, Path]:
    flexshim_root = ROOT / "flexshim"
    efi_src = flexshim_root / "target" / "x86_64-unknown-uefi" / "release" / "flexshimx64.efi"
    cfg_src = flexshim_root / "flexshim.cfg"
    efi_dst = assets_flexshim_dir / "flexshimx64.efi"
    cfg_dst = assets_flexshim_dir / "flexshim.cfg"

    if skip_build:
        if not efi_dst.is_file() or efi_dst.read_bytes()[:2] != b"MZ":
            raise RuntimeError(f"Staged flexshimx64.efi missing or invalid at {efi_dst}")
        if not cfg_dst.is_file():
            raise RuntimeError(f"Staged flexshim.cfg missing at {cfg_dst}")
        return efi_dst, cfg_dst

    cargo_exe = _find_cargo()
    subprocess.run(
        [cargo_exe, "build", "--release", "--target", "x86_64-unknown-uefi", "--bin", "flexshimx64"],
        cwd=flexshim_root,
        check=True,
    )
    if not efi_src.is_file() or efi_src.read_bytes()[:2] != b"MZ":
        raise RuntimeError(f"Compiled flexshimx64.efi missing or invalid at {efi_src}")
    if not cfg_src.is_file():
        raise RuntimeError(f"flexshim.cfg missing at {cfg_src}")

    assets_flexshim_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(efi_src, efi_dst)
    shutil.copy2(cfg_src, cfg_dst)
    return efi_dst, cfg_dst


def build_nuitka_command(
    *,
    python_exe: str,
    target_platform: str,
    qt_plugin: str,
    work_dir: Path,
    icons: dict[str, Path],
    stem: str,
) -> list[str]:
    unused_qt = "PyQt6" if qt_plugin == "pyside6" else "PySide6"
    cmd = [
        python_exe,
        "-m",
        "nuitka",
        f"--enable-plugin={qt_plugin}",
        f"--nofollow-import-to={unused_qt}",
        f"--include-data-dir={ROOT / 'src' / 'flexkit' / 'assets'}=flexkit/assets",
        f"--output-dir={work_dir}",
        f"--product-name={APP_NAME}",
        f"--product-version={__version__}",
        f"--file-version={__version__}",
        "--company-name=FlexKit",
        "--assume-yes-for-downloads",
    ]

    if target_platform == "win32":
        cmd += [
            "--onefile",
            "--windows-console-mode=disable",
            f"--windows-icon-from-ico={icons['ico']}",
            f"--output-filename={stem}.exe",
        ]
    elif target_platform == "darwin":
        cmd += [
            "--standalone",
            "--macos-create-app-bundle",
            f"--macos-app-name={APP_NAME}",
            f"--macos-app-icon={icons['icns']}",
            f"--macos-app-version={__version__}",
            f"--output-filename={APP_SLUG}.bin",
        ]
    else:
        cmd += [
            "--onefile",
            f"--output-filename={stem}.bin",
        ]

    cmd.append(str(ROOT / "src" / "flexkit" / "__main__.py"))
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile FlexKit native binary with Nuitka")
    parser.add_argument("--sign", action="store_true", help="Sign binary if platform credentials are configured")
    parser.add_argument("--qt-plugin", choices=("pyside6", "pyqt6"), default="pyside6")
    parser.add_argument("--build-flexshim-only", action="store_true", help="Build and stage flexshim UEFI assets only")
    parser.add_argument("--skip-flexshim-build", action="store_true", help="Reuse pre-staged flexshim assets")
    args = parser.parse_args(argv)

    assets_flexshim_dir = ROOT / "src" / "flexkit" / "assets" / "flexshim"
    efi_dst, cfg_dst = build_flexshim(assets_flexshim_dir, skip_build=args.skip_flexshim_build)
    if args.build_flexshim_only:
        print(f"Staged {efi_dst}")
        print(f"Staged {cfg_dst}")
        return 0

    dist_dir = ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    local_build = ROOT / "build"
    if _supports_symlinks(local_build):
        stage_root = local_build
    else:
        stage_root = Path(tempfile.gettempdir()) / "flexkit-build"
    work_dir = stage_root / "nuitka"
    work_dir.mkdir(parents=True, exist_ok=True)

    icons = ensure_platform_icons(stage_root / "icons")
    os_tag = _norm_os()
    arch_tag = _norm_arch()
    stem = f"{APP_SLUG}-{__version__}-{os_tag}-{arch_tag}"

    env = os.environ.copy()
    src_path = str(ROOT / "src")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_path

    python_exe = str(Path(sys.executable).parent.resolve() / Path(sys.executable).name)
    cmd = build_nuitka_command(
        python_exe=python_exe,
        target_platform=sys.platform,
        qt_plugin=args.qt_plugin,
        work_dir=work_dir,
        icons=icons,
        stem=stem,
    )
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)

    if sys.platform == "win32":
        artifact = dist_dir / f"{stem}.exe"
        shutil.copy2(work_dir / f"{stem}.exe", artifact)
        if args.sign and os.environ.get("WINDOWS_SIGN_THUMBPRINT"):
            subprocess.run(
                [
                    "signtool",
                    "sign",
                    "/fd",
                    "SHA256",
                    "/sha1",
                    os.environ["WINDOWS_SIGN_THUMBPRINT"],
                    "/tr",
                    "http://timestamp.digicert.com",
                    "/td",
                    "SHA256",
                    str(artifact),
                ],
                check=True,
            )
    elif sys.platform == "darwin":
        app_bundles = list(work_dir.glob("*.app"))
        if not app_bundles:
            raise RuntimeError("Nuitka did not emit a macOS .app bundle")
        app_bundle = app_bundles[0]
        if args.sign and os.environ.get("MACOS_SIGN_IDENTITY"):
            subprocess.run(
                [
                    "codesign",
                    "--deep",
                    "--force",
                    "--options",
                    "runtime",
                    "--sign",
                    os.environ["MACOS_SIGN_IDENTITY"],
                    str(app_bundle),
                ],
                check=True,
            )
        dmg_stage = stage_root / "dmg_stage"
        shutil.rmtree(dmg_stage, ignore_errors=True)
        dmg_stage.mkdir(parents=True, exist_ok=True)
        staged_app = dmg_stage / f"{APP_NAME}.app"
        shutil.copytree(app_bundle, staged_app, symlinks=True)
        (dmg_stage / "Applications").symlink_to("/Applications")

        artifact = dist_dir / f"{stem}.dmg"
        artifact.unlink(missing_ok=True)
        hdiutil_cmd = [
            "hdiutil",
            "create",
            "-volname",
            APP_NAME,
            "-srcfolder",
            str(dmg_stage),
            "-ov",
            "-format",
            "UDZO",
            str(artifact),
        ]
        for attempt in range(3):
            res = subprocess.run(hdiutil_cmd, check=False)
            if res.returncode == 0:
                break
            if attempt == 2:
                raise subprocess.CalledProcessError(res.returncode, hdiutil_cmd)
            time.sleep(2)
    else:
        artifact = dist_dir / f"{stem}.bin"
        shutil.copy2(work_dir / f"{stem}.bin", artifact)
        artifact.chmod(0o755)

    sha_file = _write_sha256(artifact)
    _ = APP_ID
    print(f"Built {artifact}")
    print(f"SHA256 {sha_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
