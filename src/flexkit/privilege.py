from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

from flexkit.writer import WriteCancelled, WriteError, WriteProgressCallback, WriteSpec

log = logging.getLogger("flexkit.privilege")

PasswordPrompt = Callable[[str | None], bytearray | None]


class ElevationCancelled(WriteCancelled):
    pass


class ElevationError(WriteError):
    pass


def is_privileged() -> bool:
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def _is_compiled() -> bool:
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def _base_helper_command(spec: WriteSpec, port: int, token: str) -> tuple[list[str], dict[str, str]]:
    args = [
        "--write",
        "--archive",
        str(spec.archive_path.resolve()),
        "--member",
        spec.member_name,
        "--device",
        spec.device,
        "--serial",
        spec.expected_serial,
        "--size",
        str(spec.expected_size),
        "--image-size",
        str(spec.image_size),
        "--sha1",
        spec.expected_sha1,
        "--port",
        str(port),
        "--token",
        token,
    ]
    extra_env: dict[str, str] = {}
    if _is_compiled():
        exe = os.environ.get("NUITKA_ONEFILE_BINARY") or os.path.abspath(sys.argv[0])
        if not os.path.isfile(exe):
            exe = sys.executable
        return [exe, *args], extra_env

    src_root = str(Path(__file__).resolve().parent.parent)
    existing_pp = os.environ.get("PYTHONPATH", "")
    extra_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing_pp}" if existing_pp else src_root
    return [sys.executable, "-m", "flexkit", *args], extra_env


def _wrap_env(cmd: list[str], extra_env: dict[str, str]) -> list[str]:
    if not extra_env:
        return cmd
    env_bin = shutil.which("env") or "/usr/bin/env"
    pairs = [f"{k}={v}" for k, v in extra_env.items()]
    return [env_bin, *pairs, *cmd]


def _can_run_passwordless(tool: str) -> bool:
    exe = shutil.which(tool)
    if not exe:
        return False
    try:
        res = subprocess.run(
            [exe, "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _zero_bytes(buf: bytearray | None) -> None:
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


def _verify_sudo_password(sudo_exe: str, pw: bytearray) -> bool:
    proc = subprocess.Popen(
        [sudo_exe, "-S", "-k", "-p", "", "true"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(bytes(pw) + b"\n")
        proc.stdin.close()
        return proc.wait(timeout=8.0) == 0
    except (OSError, subprocess.SubprocessError):
        proc.kill()
        return False


class _HelperBridge:
    def __init__(self, token: str) -> None:
        self._token = token
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port: int = int(self._srv.getsockname()[1])
        self._client: socket.socket | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except OSError:
                    pass
                self._client = None
        try:
            self._srv.close()
        except OSError:
            pass

    def request_cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._client is not None:
                try:
                    self._client.sendall(b'{"cmd":"cancel"}\n')
                except OSError:
                    pass

    def accept_authenticated(self, proc_alive: Callable[[], bool], timeout: float = 120.0) -> socket.socket | None:
        self._srv.settimeout(0.25)
        start_t = time.monotonic()
        while True:
            if self._cancelled:
                return None
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                if not proc_alive():
                    return None
                if (time.monotonic() - start_t) > timeout:
                    return None
                continue
            except OSError:
                return None

            conn.settimeout(3.0)
            try:
                buf = b""
                while b"\n" not in buf and len(buf) < 4096:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                if b"\n" not in buf:
                    conn.close()
                    continue
                first_line, rest = buf.split(b"\n", 1)
                hello = json.loads(first_line.decode("utf-8"))
                got_tok = str(hello.get("token") or "")
                if hello.get("event") != "hello" or not hmac.compare_digest(got_tok, self._token):
                    conn.close()
                    continue
                conn.settimeout(None)
                with self._lock:
                    self._client = conn
                    if self._cancelled:
                        try:
                            conn.sendall(b'{"cmd":"cancel"}\n')
                        except OSError:
                            pass
                self._initial_rest = rest
                return conn
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                conn.close()
                continue

    def consume_events(self, conn: socket.socket, progress_cb: WriteProgressCallback | None) -> None:
        buf = getattr(self, "_initial_rest", b"")
        while True:
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                if not raw_line.strip():
                    continue
                msg = json.loads(raw_line.decode("utf-8"))
                ev = msg.get("event")
                if ev == "progress":
                    if progress_cb is not None:
                        progress_cb(
                            str(msg.get("phase") or "write"),
                            int(msg.get("done") or 0),
                            int(msg.get("total") or 0),
                            int(msg.get("bps") or 0),
                        )
                elif ev == "done":
                    return
                elif ev == "cancelled":
                    raise WriteCancelled()
                elif ev == "error":
                    raise WriteError(str(msg.get("message") or "Write failed."))

            chunk = conn.recv(4096)
            if not chunk:
                if self._cancelled:
                    raise WriteCancelled()
                raise WriteError("Privileged writer terminated unexpectedly.")
            buf += chunk


class _SHELL_EXECUTE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _spawn_windows_uac(cmd: list[str], extra_env: dict[str, str]) -> tuple[Callable[[], bool], Callable[[], None]]:
    if extra_env:
        for k, v in extra_env.items():
            os.environ[k] = v
    exe = cmd[0]
    params = subprocess.list2cmdline(cmd[1:])
    sei = _SHELL_EXECUTE_INFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = 0

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = kernel32.GetLastError()
        if err == 1223:
            raise ElevationCancelled()
        raise ElevationError(f"Administrator elevation failed (code {err}).")

    h_proc = sei.hProcess

    def is_alive() -> bool:
        return kernel32.WaitForSingleObject(h_proc, 0) == 0x00000102

    def cleanup() -> None:
        if h_proc:
            kernel32.WaitForSingleObject(h_proc, 3000)
            kernel32.CloseHandle(h_proc)

    return is_alive, cleanup


def run_privileged_write(
    spec: WriteSpec,
    *,
    password_prompt: PasswordPrompt | None = None,
    progress_cb: WriteProgressCallback | None = None,
    register_cancel: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    token = secrets.token_hex(32)
    bridge = _HelperBridge(token)
    if register_cancel is not None:
        register_cancel(bridge.request_cancel)

    cmd, extra_env = _base_helper_command(spec, bridge.port, token)
    wrapped = _wrap_env(cmd, extra_env)

    try:
        if is_privileged():
            env = {**os.environ, **extra_env}
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                if conn is None:
                    err = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "").strip()
                    raise ElevationError(err or "Helper failed to start.")
                bridge.consume_events(conn, progress_cb)
            finally:
                proc.wait(timeout=5.0)
            return

        if sys.platform == "win32":
            alive_fn, cleanup_fn = _spawn_windows_uac(cmd, extra_env)
            try:
                conn = bridge.accept_authenticated(alive_fn)
                if conn is None:
                    raise ElevationError("Elevated writer did not connect.")
                bridge.consume_events(conn, progress_cb)
            finally:
                cleanup_fn()
            return

        for tool in ("sudo", "doas"):
            if _can_run_passwordless(tool):
                exe = shutil.which(tool)
                assert exe is not None
                proc = subprocess.Popen(
                    [exe, "-n", *wrapped],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                try:
                    conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                    if conn is not None:
                        bridge.consume_events(conn, progress_cb)
                        return
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5.0)

        if sys.platform == "darwin" and shutil.which("osascript"):
            shell_cmd = " ".join(shlex.quote(part) for part in wrapped)
            escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
            proc = subprocess.Popen(
                ["osascript", "-e", f'do shell script "{escaped}" with administrator privileges'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                if conn is not None:
                    bridge.consume_events(conn, progress_cb)
                    return
                proc.wait(timeout=5.0)
                stderr_txt = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "").lower()
                if "canceled" in stderr_txt or "-128" in stderr_txt:
                    raise ElevationCancelled()
            finally:
                if proc.poll() is None:
                    proc.terminate()

        pkexec_exe = shutil.which("pkexec")
        if pkexec_exe and sys.platform != "darwin":
            proc = subprocess.Popen(
                [pkexec_exe, *wrapped],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                if conn is not None:
                    bridge.consume_events(conn, progress_cb)
                    return
                rc = proc.wait(timeout=5.0)
                stderr_txt = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "").lower()
                if rc == 126 or "dismissed" in stderr_txt:
                    raise ElevationCancelled()
                log.info("pkexec unavailable in session (rc=%s), falling back to password prompt", rc)
            finally:
                if proc.poll() is None:
                    proc.terminate()

        for gui_sudo, flag in (("kdesu", "-c"), ("lxqt-sudo", "-s")):
            gui_exe = shutil.which(gui_sudo)
            if gui_exe and os.environ.get("DISPLAY"):
                full_cmd = (
                    [gui_exe, flag, " ".join(shlex.quote(x) for x in wrapped)]
                    if flag == "-c"
                    else [gui_exe, flag, *wrapped]
                )
                proc = subprocess.Popen(
                    full_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                try:
                    conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                    if conn is not None:
                        bridge.consume_events(conn, progress_cb)
                        return
                finally:
                    if proc.poll() is None:
                        proc.terminate()

        sudo_exe = shutil.which("sudo")
        if not sudo_exe:
            raise ElevationError("Administrator privileges required, and neither pkexec nor sudo was found.")
        if password_prompt is None:
            raise ElevationError("Authentication required to write to USB drive.")

        prompt_err: str | None = None
        for _ in range(3):
            pw = password_prompt(prompt_err)
            if pw is None:
                raise ElevationCancelled()
            try:
                if not _verify_sudo_password(sudo_exe, pw):
                    prompt_err = "Incorrect password. Try again."
                    continue
                proc = subprocess.Popen(
                    [sudo_exe, "-S", "-k", "-p", "", *wrapped],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(bytes(pw) + b"\n")
                    proc.stdin.close()
                    conn = bridge.accept_authenticated(lambda: proc.poll() is None)
                    if conn is None:
                        err_out = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "").strip()
                        raise ElevationError(err_out or "Failed to launch privileged writer.")
                    bridge.consume_events(conn, progress_cb)
                    return
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5.0)
            finally:
                _zero_bytes(pw)

        raise ElevationError("Authentication failed after 3 attempts.")
    finally:
        bridge.close()
