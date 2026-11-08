from __future__ import annotations

from contextlib import AbstractContextManager
import ctypes
import logging
import os
import shutil
import subprocess
import sys

from flexkit import APP_NAME

log = logging.getLogger("flexkit.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class SleepInhibitor(AbstractContextManager["SleepInhibitor"]):
    def __init__(self, reason: str = "Creating ChromeOS Flex USB installer") -> None:
        self._reason = reason
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> SleepInhibitor:
        try:
            if sys.platform == "win32":
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            elif sys.platform == "darwin":
                exe = shutil.which("caffeinate")
                if exe:
                    self._proc = subprocess.Popen(
                        [exe, "-i", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            elif sys.platform.startswith("linux"):
                exe = shutil.which("systemd-inhibit")
                if exe:
                    self._proc = subprocess.Popen(
                        [
                            exe,
                            f"--who={APP_NAME}",
                            f"--why={self._reason}",
                            "--what=idle:sleep",
                            "--mode=block",
                            "cat",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        except Exception as exc:
            log.debug("Sleep inhibition unavailable: %s", exc)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if sys.platform == "win32":
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            if self._proc is not None:
                if self._proc.stdin is not None:
                    try:
                        self._proc.stdin.close()
                    except OSError:
                        pass
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                self._proc = None
        except Exception:
            pass
