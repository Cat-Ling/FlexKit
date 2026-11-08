from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

__version__ = "0.0.1"

PROGRAM_NAME = "FlexKit"
APP_NAME = "ChromeOS Flex Installer"
APP_SLUG = "flexkit"
APP_ID = "io.github.flexkit"
USER_AGENT = f"{PROGRAM_NAME}/{__version__}"


def assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def user_cache_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return base / "FlexKit" / "Cache"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "FlexKit"
    base = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache"))
    return base / APP_SLUG


def user_state_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return base / "FlexKit"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "FlexKit"
    base = Path(os.environ.get("XDG_STATE_HOME") or (home / ".local" / "state"))
    return base / APP_SLUG


def default_download_dir() -> Path:
    home = Path.home()
    if sys.platform not in ("win32", "darwin"):
        cfg_root = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        user_dirs = cfg_root / "user-dirs.dirs"
        if user_dirs.is_file():
            try:
                content = user_dirs.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]+)"', content, re.MULTILINE)
                if m:
                    raw = m.group(1).replace("$HOME", str(home))
                    candidate = Path(raw)
                    if candidate.is_dir():
                        return candidate
            except OSError:
                pass
    downloads = home / "Downloads"
    return downloads if downloads.is_dir() else home


def configure_logging(*, helper_mode: bool = False) -> logging.Logger:
    logger = logging.getLogger(APP_SLUG)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    if not helper_mode:
        try:
            log_dir = user_state_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir / "flexkit.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            logger.addHandler(logging.NullHandler())
    else:
        logger.addHandler(logging.NullHandler())
    return logger
