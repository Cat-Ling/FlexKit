from __future__ import annotations

import argparse
import sys

from flexkit import (
    APP_ID,
    APP_NAME,
    PROGRAM_NAME,
    __version__,
    configure_logging,
    set_log_settings,
    user_state_dir,
)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    log_arg: str | None = None
    log_file_arg: str | None = None
    i = 0
    while i < len(raw_args):
        a = raw_args[i]
        if a == "--log":
            if i + 1 < len(raw_args) and not raw_args[i + 1].startswith("-"):
                log_arg = raw_args[i + 1]
                i += 1
            else:
                log_arg = "debug"
        elif a.startswith("--log="):
            log_arg = a.split("=", 1)[1]
        elif a == "--log-file" and i + 1 < len(raw_args):
            log_file_arg = raw_args[i + 1]
            i += 1
        elif a.startswith("--log-file="):
            log_file_arg = a.split("=", 1)[1]
        i += 1

    if log_arg or log_file_arg:
        set_log_settings(log_arg, log_file_arg)

    if "--write" in raw_args or "--helper-daemon" in raw_args:
        configure_logging(helper_mode=True, log_level=log_arg, log_file=log_file_arg)
        from flexkit.writer import run_writer_cli

        return run_writer_cli(raw_args)

    parser = argparse.ArgumentParser(prog="flexkit", description=APP_NAME)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument(
        "--log",
        nargs="?",
        const="debug",
        default=None,
        help="Enable file logging (levels: debug, info, warn, error; or path to log file)",
    )
    parser.add_argument("--log-file", default=None, help="Path to write log file (default: log.txt)")
    parser.add_argument("--no-aria2", action="store_true", help="Use built-in HTTP downloader even if aria2c is installed")
    parser.add_argument("--self-test-gui", action="store_true", help=argparse.SUPPRESS)
    args, qt_extra = parser.parse_known_args(raw_args)

    if args.version:
        print(f"{PROGRAM_NAME} {__version__}")
        return 0

    main_log = configure_logging(
        helper_mode=False,
        log_level=log_arg or args.log,
        log_file=log_file_arg or args.log_file,
    )
    import os
    main_log.info("FlexKit v%s starting (pid=%d, argv=%s)", __version__, os.getpid(), sys.argv)

    from flexkit.ui.qt import QtCore, QtWidgets
    from flexkit.ui.theme import apply_theme
    from flexkit.ui.window import FlexKitWindow

    app = QtWidgets.QApplication([sys.argv[0], *qt_extra])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    set_desktop = getattr(app, "setDesktopFileName", None)
    if set_desktop is not None:
        set_desktop(APP_ID)

    apply_theme(app)

    state_dir = user_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = QtCore.QLockFile(str(state_dir / "flexkit.lock"))
    lock.setStaleLockTime(0)
    if not args.self_test_gui and not lock.tryLock(100):
        msg = QtWidgets.QMessageBox()
        msg.setWindowTitle(APP_NAME)
        msg.setText(f"{APP_NAME} is already running.")
        msg.exec()
        return 1

    try:
        win = FlexKitWindow(use_aria2=not args.no_aria2)
        win.show()
        if args.self_test_gui:
            QtCore.QTimer.singleShot(250, win.close)
        return int(app.exec())
    finally:
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
