from __future__ import annotations

import argparse
import sys

from flexkit import APP_ID, APP_NAME, PROGRAM_NAME, __version__, configure_logging, user_state_dir


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if "--write" in raw_args:
        configure_logging(helper_mode=True)
        from flexkit.writer import run_writer_cli

        return run_writer_cli(raw_args)

    parser = argparse.ArgumentParser(prog="flexkit", description=APP_NAME)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--no-aria2", action="store_true", help="Use built-in HTTP downloader even if aria2c is installed")
    parser.add_argument("--self-test-gui", action="store_true", help=argparse.SUPPRESS)
    args, qt_extra = parser.parse_known_args(raw_args)

    if args.version:
        print(f"{PROGRAM_NAME} {__version__}")
        return 0

    configure_logging(helper_mode=False)

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
    if not lock.tryLock(100):
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
