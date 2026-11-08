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


_GLOBAL_LOG_LEVEL: str | None = None
_GLOBAL_LOG_FILE: Path | None = None


def set_log_settings(
    level_arg: str | None = None,
    file_arg: str | Path | None = None,
) -> tuple[str, Path]:
    global _GLOBAL_LOG_LEVEL, _GLOBAL_LOG_FILE

    target_level = "DEBUG"
    target_file: Path | None = None

    if level_arg:
        norm = level_arg.strip()
        lower = norm.lower()
        if lower in ("debug", "dbg"):
            target_level = "DEBUG"
        elif lower in ("info", "log"):
            target_level = "INFO"
        elif lower in ("warn", "warning"):
            target_level = "WARNING"
        elif lower in ("error", "err"):
            target_level = "ERROR"
        elif norm.endswith((".txt", ".log")) or "/" in norm or "\\" in norm:
            target_file = Path(norm).resolve()
            target_level = "DEBUG"

    if file_arg:
        target_file = Path(file_arg).resolve()

    if target_file is None:
        try:
            cwd_file = Path.cwd() / "log.txt"
            with open(cwd_file, "a", encoding="utf-8"):
                pass
            target_file = cwd_file
        except OSError:
            target_file = user_state_dir() / "log.txt"

    _GLOBAL_LOG_LEVEL = target_level
    _GLOBAL_LOG_FILE = target_file
    return target_level, target_file


def get_log_settings() -> tuple[str | None, Path | None]:
    return _GLOBAL_LOG_LEVEL, _GLOBAL_LOG_FILE


def configure_logging(
    *,
    helper_mode: bool = False,
    log_level: str | int | None = None,
    log_file: str | Path | None = None,
) -> logging.Logger:
    logger = logging.getLogger(APP_SLUG)

    lvl_name = "INFO"
    if log_level is not None:
        if isinstance(log_level, int):
            int_level = log_level
            lvl_name = logging.getLevelName(int_level)
        else:
            set_lvl, _ = set_log_settings(str(log_level), log_file)
            lvl_name = set_lvl
            int_level = getattr(logging, lvl_name, logging.INFO)
    elif _GLOBAL_LOG_LEVEL is not None:
        lvl_name = _GLOBAL_LOG_LEVEL
        int_level = getattr(logging, lvl_name, logging.INFO)
    else:
        int_level = logging.INFO

    dest_file = Path(log_file).resolve() if log_file else _GLOBAL_LOG_FILE
    if dest_file is None and not helper_mode:
        dest_file = user_state_dir() / "flexkit.log"

    logger.setLevel(int_level)
    fmt = logging.Formatter(
        "%(asctime)s [%(process)d:%(threadName)s] %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Clear existing handlers to allow reconfiguration
    for h in list(logger.handlers):
        logger.removeHandler(h)

    if dest_file is not None:
        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                dest_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setLevel(int_level)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass

    if not helper_mode or _GLOBAL_LOG_LEVEL is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(int_level)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger
