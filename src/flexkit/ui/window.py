from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import logging
from pathlib import Path
import threading

from flexkit import APP_NAME, PROGRAM_NAME, __version__, default_download_dir
from flexkit.catalog import CatalogError, Release, fetch_catalog
from flexkit.download import (
    BaseDownloader,
    DownloadCancelled,
    DownloadError,
    DownloadState,
    PreparedImage,
    VerificationError,
    create_downloader,
    inspect_existing_download,
    prepare_local_image,
    verify_archive,
)
from flexkit.drives import Drive, DriveEntry, list_drives
from flexkit.power import SleepInhibitor
from flexkit.privilege import ElevationCancelled, close_privileged_session, run_privileged_write
from flexkit.ui import theme
from flexkit.ui.dialogs import confirm_stop_write_dialog, confirm_write_dialog, password_dialog
from flexkit.ui.pages import DonePage, DrivePage, ProgressPage, SetupPage, WelcomePage
from flexkit.ui.qt import QSize, QThread, QTimer, QUrl, Qt, QtCore, QtGui, QtWidgets, Signal
from flexkit.ui.widgets import BadgeIcon, FilledButton, OutlinedButton, TextButton
from flexkit.writer import WriteCancelled, WriteSpec

log = logging.getLogger("flexkit.ui")

WIDE_BREAKPOINT = 640
MAX_STAGE_WIDTH = 940
BRAND_VERSION = f"{PROGRAM_NAME} v{__version__}"


class Step(StrEnum):
    WELCOME = "welcome"
    SETUP = "setup"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    DRIVES = "drives"
    WRITING = "writing"
    DONE = "done"


class CatalogWorker(QThread):
    loaded = Signal(object, bool)
    failed = Signal(str)

    def run(self) -> None:
        try:
            releases, from_cache = fetch_catalog()
            self.loaded.emit(releases, from_cache)
        except Exception as exc:
            self.failed.emit(str(exc))


class DownloadWorker(QThread):
    progress = Signal(object, object, object)
    status_changed = Signal(str)
    completed = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, release: Release, dest_dir: Path, *, use_aria2: bool) -> None:
        super().__init__()
        self.downloader: BaseDownloader = create_downloader(
            release,
            dest_dir,
            use_aria2=use_aria2,
            progress_cb=lambda d, t, b: self.progress.emit(d, t, b),
            status_cb=lambda msg: self.status_changed.emit(msg),
        )

    def run(self) -> None:
        with SleepInhibitor("Downloading ChromeOS Flex"):
            try:
                path = self.downloader.run()
                self.completed.emit(path)
            except DownloadCancelled:
                self.cancelled.emit()
            except Exception as exc:
                self.failed.emit(str(exc))


class VerifyWorker(QThread):
    progress = Signal(object, object, object)
    completed = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        source_path: Path,
        release: Release | None = None,
        *,
        catalog: list[Release] | tuple[Release, ...] = (),
    ) -> None:
        super().__init__()
        self._source_path = source_path
        self._release = release
        self._catalog = tuple(catalog)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        with SleepInhibitor("Verifying ChromeOS Flex image"):
            try:
                if self._release is not None:
                    prepared = verify_archive(
                        self._source_path,
                        self._release,
                        metrics_cb=lambda d, t, b: self.progress.emit(d, t, b),
                        cancel_check=self._cancel.is_set,
                    )
                else:
                    prepared = prepare_local_image(
                        self._source_path,
                        self._catalog,
                        metrics_cb=lambda d, t, b: self.progress.emit(d, t, b),
                        cancel_check=self._cancel.is_set,
                    )
                self.completed.emit(prepared)
            except DownloadCancelled:
                self.cancelled.emit()
            except (VerificationError, OSError) as exc:
                self.failed.emit(str(exc))
            except Exception as exc:
                self.failed.emit(str(exc))


class DriveScanWorker(QThread):
    scanned = Signal(object)

    def __init__(
        self,
        include_hdd: bool,
        image_path: Path | None,
        min_size: int,
        *,
        is_local: bool = False,
    ) -> None:
        super().__init__()
        self._include_hdd = include_hdd
        self._image_path = image_path
        self._min_size = min_size
        self._is_local = is_local

    def run(self) -> None:
        entries = list_drives(
            include_usb_hdd=self._include_hdd,
            image_path=self._image_path,
            min_size=self._min_size,
            is_local_image=self._is_local,
        )
        self.scanned.emit(entries)


class WriteWorker(QThread):
    progress = Signal(str, object, object, object)
    password_needed = Signal(object)
    completed = Signal()
    cancelled = Signal(bool)
    failed = Signal(str)

    def __init__(self, spec: WriteSpec) -> None:
        super().__init__()
        self._spec = spec
        self._pw_event = threading.Event()
        self._pw_result: bytearray | None = None
        self._cancel_fn: Callable[[], None] | None = None
        self._started_writing = False

    def provide_password(self, pw: bytearray | None) -> None:
        self._pw_result = pw
        self._pw_event.set()

    def request_cancel(self) -> None:
        if self._cancel_fn is not None:
            self._cancel_fn()

    def _prompt_password(self, error_msg: str | None) -> bytearray | None:
        self._pw_event.clear()
        self.password_needed.emit(error_msg)
        self._pw_event.wait()
        res = self._pw_result
        self._pw_result = None
        return res

    def _on_progress(self, phase: str, done: int, total: int, bps: int) -> None:
        if done > 0:
            self._started_writing = True
        self.progress.emit(phase, done, total, bps)

    def run(self) -> None:
        with SleepInhibitor("Writing ChromeOS Flex USB installer"):
            try:
                log.info(
                    "Starting write worker for %s (size=%d, mode=%s, verify=%s)",
                    self._spec.device,
                    self._spec.image_size,
                    self._spec.install_mode,
                    self._spec.verify_drive,
                )
                run_privileged_write(
                    self._spec,
                    password_prompt=self._prompt_password,
                    progress_cb=self._on_progress,
                    register_cancel=lambda fn: setattr(self, "_cancel_fn", fn),
                )
                log.info("Write worker finished successfully.")
                self.completed.emit()
            except ElevationCancelled:
                log.warning("Write worker cancelled by user during elevation.")
                self.cancelled.emit(False)
            except WriteCancelled:
                log.warning("Write worker cancelled by user during flashing.")
                self.cancelled.emit(self._started_writing)
            except Exception as exc:
                log.exception("Write worker failed: %s", exc)
                self.failed.emit(str(exc))


class FlexKitWindow(QtWidgets.QMainWindow):
    def __init__(self, *, use_aria2: bool = True) -> None:
        super().__init__()
        self._use_aria2 = use_aria2
        self._step = Step.WELCOME
        self._releases: list[Release] = []
        self._active_release: Release | None = None
        self._prepared: PreparedImage | None = None
        self._archive_path: Path | None = None
        self._verifying_label: str = ""
        self._verifying_local: bool = False
        self._target_drive: Drive | None = None
        self._download_only = False

        self._active_threads: set[QThread] = set()
        self._cat_worker: CatalogWorker | None = None
        self._dl_worker: DownloadWorker | None = None
        self._ver_worker: VerifyWorker | None = None
        self._scan_worker: DriveScanWorker | None = None
        self._write_worker: WriteWorker | None = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.render_app_icon())
        self.setMinimumSize(380, 500)
        self.resize(760, 500)

        central = QtWidgets.QWidget(self)
        central.setObjectName("centralRoot")
        central.setStyleSheet(f"#centralRoot {{ background-color: {theme.SURFACE}; }}")
        self.setCentralWidget(central)

        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body_outer = QtWidgets.QWidget(central)
        body_outer_lay = QtWidgets.QHBoxLayout(body_outer)
        body_outer_lay.setContentsMargins(0, 0, 0, 0)
        body_outer_lay.setSpacing(0)
        body_outer_lay.addStretch(1)

        self._body_host = QtWidgets.QWidget(body_outer)
        self._body_host.setMaximumWidth(MAX_STAGE_WIDTH)
        self._body_host.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self._body_grid = QtWidgets.QGridLayout(self._body_host)
        self._body_grid.setContentsMargins(36, 32, 36, 20)
        self._body_grid.setHorizontalSpacing(36)
        self._body_grid.setVerticalSpacing(18)
        body_outer_lay.addWidget(self._body_host, 100)
        body_outer_lay.addStretch(1)
        root_layout.addWidget(body_outer, 1)

        self._header_pane = QtWidgets.QWidget(self._body_host)
        h_lay = QtWidgets.QVBoxLayout(self._header_pane)
        h_lay.setContentsMargins(0, 0, 0, 0)
        h_lay.setSpacing(14)

        top_badge_row = QtWidgets.QHBoxLayout()
        top_badge_row.setContentsMargins(0, 0, 0, 0)
        self._badge = BadgeIcon("flex", box_size=56, parent=self._header_pane)
        top_badge_row.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignLeft)
        top_badge_row.addStretch(1)
        self._compact_ver_label = QtWidgets.QLabel(BRAND_VERSION, self._header_pane)
        self._compact_ver_label.setFont(theme.make_font(12, QtGui.QFont.Weight.Medium))
        self._compact_ver_label.setStyleSheet(f"color: {theme.OUTLINE};")
        self._compact_ver_label.hide()
        top_badge_row.addWidget(self._compact_ver_label, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        h_lay.addLayout(top_badge_row)

        self._headline = QtWidgets.QLabel("", self._header_pane)
        self._headline.setWordWrap(True)
        self._headline.setFont(theme.make_font(30, QtGui.QFont.Weight.Normal))
        h_lay.addWidget(self._headline)

        self._subtitle = QtWidgets.QLabel("", self._header_pane)
        self._subtitle.setWordWrap(True)
        self._subtitle.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
        self._subtitle.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT};")
        h_lay.addWidget(self._subtitle)

        self._header_stretch = QtWidgets.QSpacerItem(
            0, 0, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding
        )
        h_lay.addItem(self._header_stretch)

        self._ver_label = QtWidgets.QLabel(BRAND_VERSION, self._header_pane)
        self._ver_label.setFont(theme.make_font(12, QtGui.QFont.Weight.Medium))
        self._ver_label.setStyleSheet(f"color: {theme.OUTLINE};")
        h_lay.addWidget(self._ver_label)

        self._stack = QtWidgets.QStackedWidget(self._body_host)
        self.welcome_page = WelcomePage(self._stack)
        self.setup_page = SetupPage(default_download_dir(), self._stack)
        self.progress_page = ProgressPage(self._stack)
        self.drive_page = DrivePage(self._stack)
        self.done_page = DonePage(self._stack)

        for page in (
            self.welcome_page,
            self.setup_page,
            self.progress_page,
            self.drive_page,
            self.done_page,
        ):
            self._stack.addWidget(page)

        divider = QtWidgets.QFrame(central)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {theme.DIVIDER};")
        root_layout.addWidget(divider)

        footer = QtWidgets.QWidget(central)
        footer.setFixedHeight(76)
        footer_outer = QtWidgets.QHBoxLayout(footer)
        footer_outer.setContentsMargins(0, 0, 0, 0)
        footer_outer.setSpacing(0)
        footer_outer.addStretch(1)

        self._footer_host = QtWidgets.QWidget(footer)
        self._footer_host.setMaximumWidth(MAX_STAGE_WIDTH)
        self._footer_host.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self._footer_lay = QtWidgets.QHBoxLayout(self._footer_host)
        self._footer_lay.setContentsMargins(36, 0, 36, 0)
        self._footer_lay.setSpacing(10)

        self._left_btn = TextButton("", self._footer_host)
        self._mid_btn = OutlinedButton("", self._footer_host)
        self._primary_btn = FilledButton("", self._footer_host)

        self._left_btn.clicked.connect(self._on_left_action)
        self._mid_btn.clicked.connect(self._on_mid_action)
        self._primary_btn.clicked.connect(self._on_primary_action)

        self._footer_lay.addWidget(self._left_btn, 0)
        self._footer_lay.addStretch(1)
        self._footer_lay.addWidget(self._mid_btn, 0)
        self._footer_lay.addWidget(self._primary_btn, 0)

        footer_outer.addWidget(self._footer_host, 100)
        footer_outer.addStretch(1)
        root_layout.addWidget(footer)

        self.setup_page.selection_changed.connect(self._refresh_setup_state)
        self.drive_page.selection_changed.connect(self._refresh_drive_actions)
        self.drive_page.filter_toggled.connect(lambda _c: self._trigger_drive_scan())
        self.drive_page.refresh_requested.connect(self._trigger_drive_scan)

        self._drive_poll = QTimer(self)
        self._drive_poll.setInterval(2000)
        self._drive_poll.timeout.connect(self._trigger_drive_scan)

        self._is_wide: bool | None = None
        self._apply_responsive_layout()
        self._go_to(Step.WELCOME)
        self._start_catalog_fetch()

    def _launch_worker(self, worker: QThread) -> None:
        self._active_threads.add(worker)
        worker.finished.connect(lambda w=worker: self._retire_worker(w))
        worker.start()

    def _retire_worker(self, worker: QThread) -> None:
        self._active_threads.discard(worker)
        if self._cat_worker is worker:
            self._cat_worker = None
        if self._scan_worker is worker:
            self._scan_worker = None
        if self._dl_worker is worker:
            self._dl_worker = None
        if self._ver_worker is worker:
            self._ver_worker = None
        if self._write_worker is worker:
            self._write_worker = None
        worker.deleteLater()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        wide = self.width() >= WIDE_BREAKPOINT
        stage_w = min(self.width(), MAX_STAGE_WIDTH)

        if wide:
            self._header_pane.setFixedWidth(min(300, max(245, int(stage_w * 0.37))))

        if wide == self._is_wide:
            return
        self._is_wide = wide

        self._body_grid.removeWidget(self._header_pane)
        self._body_grid.removeWidget(self._stack)

        if wide:
            self._body_grid.setContentsMargins(36, 32, 36, 20)
            self._footer_lay.setContentsMargins(36, 0, 36, 0)
            self._badge.set_box_size(56)
            self._headline.setFont(theme.make_font(30, QtGui.QFont.Weight.Normal))
            self._ver_label.show()
            self._compact_ver_label.hide()
            self._body_grid.addWidget(self._header_pane, 0, 0)
            self._body_grid.addWidget(self._stack, 0, 1)
            self._body_grid.setColumnStretch(0, 0)
            self._body_grid.setColumnStretch(1, 1)
            self._body_grid.setRowStretch(0, 1)
            self._body_grid.setRowStretch(1, 0)
        else:
            self._body_grid.setContentsMargins(20, 20, 20, 12)
            self._footer_lay.setContentsMargins(18, 0, 18, 0)
            self._header_pane.setMinimumWidth(0)
            self._header_pane.setMaximumWidth(16777215)
            self._badge.set_box_size(46)
            self._headline.setFont(theme.make_font(24, QtGui.QFont.Weight.Normal))
            self._ver_label.hide()
            self._compact_ver_label.show()
            self._body_grid.addWidget(self._header_pane, 0, 0)
            self._body_grid.addWidget(self._stack, 1, 0)
            self._body_grid.setColumnStretch(0, 1)
            self._body_grid.setColumnStretch(1, 0)
            self._body_grid.setRowStretch(0, 0)
            self._body_grid.setRowStretch(1, 1)

    def _set_header(
        self,
        glyph: str,
        headline: str,
        subtitle: str = "",
        *,
        bg_hex: str = theme.PRIMARY_CONTAINER,
        fg_hex: str = theme.PRIMARY,
    ) -> None:
        self._badge.set_spec(glyph, bg_hex, fg_hex)
        self._headline.setText(headline)
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))

    def _release_tag(self) -> str:
        rel = self._active_release
        return f"ChromeOS Flex {rel.version}" if rel else "ChromeOS Flex"

    def _go_to(self, step: Step) -> None:
        log.info("Transitioned to step: %s", step.value)
        self._step = step
        if step != Step.DRIVES:
            self._drive_poll.stop()

        for btn in (self._left_btn, self._mid_btn, self._primary_btn):
            btn.clearFocus()
        self._left_btn.hide()
        self._mid_btn.hide()
        self._primary_btn.show()
        self._primary_btn.setEnabled(True)

        if step == Step.WELCOME:
            self._set_header(
                "flex",
                "ChromeOS Flex",
                "Download ChromeOS Flex and create a USB installer.",
            )
            self._stack.setCurrentWidget(self.welcome_page)
            self._mid_btn.setText("Use local image")
            self._mid_btn.show()
            self._primary_btn.setText("Get started")

        elif step == Step.SETUP:
            self._set_header("download", "Download ChromeOS Flex")
            self._stack.setCurrentWidget(self.setup_page)
            self._left_btn.setText("Back")
            self._left_btn.show()
            self._mid_btn.setText("Use local image")
            self._mid_btn.show()
            self._refresh_setup_state()

        elif step == Step.DOWNLOADING:
            self._set_header("download", "Downloading", self._release_tag())
            self._stack.setCurrentWidget(self.progress_page)
            self._left_btn.setText("Cancel")
            self._left_btn.show()
            self._mid_btn.setText("Pause")
            self._mid_btn.show()
            self._primary_btn.hide()

        elif step == Step.VERIFYING:
            self._set_header("verify", "Verifying", self._verifying_label or self._release_tag())
            self._stack.setCurrentWidget(self.progress_page)
            self._left_btn.setText("Cancel")
            self._left_btn.show()
            self._primary_btn.hide()

        elif step == Step.DRIVES:
            sub = self._prepared.title if (self._prepared and self._prepared.is_local) else "16 GB or larger"
            self._set_header("usb", "Choose a USB drive", sub)
            self._stack.setCurrentWidget(self.drive_page)
            is_local = bool(self._prepared and self._prepared.is_local)
            self._left_btn.setText("Back" if is_local else "Skip")
            self._left_btn.show()
            self._primary_btn.setText("Next")
            self._refresh_drive_actions()
            self._trigger_drive_scan()
            self._drive_poll.start()

        elif step == Step.WRITING:
            drv_name = self._target_drive.name if self._target_drive else "USB drive"
            self._set_header("usb", f"Writing to {drv_name}", "Don't remove the drive.")
            self._stack.setCurrentWidget(self.progress_page)
            self._left_btn.setText("Cancel")
            self._left_btn.show()
            self._primary_btn.hide()

        elif step == Step.DONE:
            if self._download_only:
                saved_in = str(self._archive_path.parent) if self._archive_path else ""
                self._set_header(
                    "check",
                    "Download complete",
                    saved_in,
                    bg_hex=theme.SUCCESS_CONTAINER,
                    fg_hex=theme.SUCCESS,
                )
                self._mid_btn.setText("Show in folder")
                self._mid_btn.show()
            else:
                self._set_header(
                    "check",
                    "Your USB installer is ready",
                    "You can now remove the drive.",
                    bg_hex=theme.SUCCESS_CONTAINER,
                    fg_hex=theme.SUCCESS,
                )
            self._stack.setCurrentWidget(self.done_page)
            self._primary_btn.setText("Done")

    def _start_catalog_fetch(self) -> None:
        if self._cat_worker is not None and self._cat_worker.isRunning():
            return
        worker = CatalogWorker()
        self._cat_worker = worker
        worker.loaded.connect(self._on_catalog_loaded)
        worker.failed.connect(self._on_catalog_failed)
        self._launch_worker(worker)

    def _on_catalog_loaded(self, releases: list[Release], from_cache: bool) -> None:
        self._releases = releases
        self.setup_page.set_releases(releases)
        if from_cache:
            self.setup_page.banner.show_message("Offline — using cached catalog.", kind="warning")
        else:
            self.setup_page.banner.show_message("")
        self._refresh_setup_state()

    def _on_catalog_failed(self, err: str) -> None:
        self.setup_page.banner.show_message(err, kind="error")
        self._refresh_setup_state()

    def _refresh_setup_state(self) -> None:
        if self._step != Step.SETUP:
            return
        if self._cat_worker is not None and self._cat_worker.isRunning() and not self._releases:
            self._primary_btn.setText("Loading…")
            self._primary_btn.setEnabled(False)
            return

        rel = self.setup_page.current_release()
        self.setup_page.update_summary(rel)
        if rel is None:
            self._primary_btn.setText("Retry")
            self._primary_btn.setEnabled(True)
            return

        dest = self.setup_page.path_selector.path
        dl_state, _ = inspect_existing_download(dest, rel, allow_aria2=self._use_aria2)
        if dl_state == DownloadState.COMPLETE:
            self._primary_btn.setText("Continue")
        elif dl_state == DownloadState.RESUMABLE:
            self._primary_btn.setText("Resume")
        else:
            self._primary_btn.setText("Download")
        self._primary_btn.setEnabled(True)

    def _refresh_drive_actions(self) -> None:
        if self._step == Step.DRIVES:
            self._primary_btn.setEnabled(self.drive_page.selected_drive() is not None)

    def _choose_local_image(self) -> None:
        picked, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select ChromeOS Flex image",
            str(self.setup_page.path_selector.path),
            "Disk images (*.zip *.bin *.img);;All files (*)",
        )
        if not picked:
            return
        self.setup_page.banner.show_message("")
        self._start_local_verification(Path(picked))

    def _on_left_action(self) -> None:
        if self._step == Step.SETUP:
            self._go_to(Step.WELCOME)
        elif self._step == Step.DOWNLOADING:
            if self._dl_worker is not None:
                self._left_btn.setEnabled(False)
                self._dl_worker.downloader.cancel()
        elif self._step == Step.VERIFYING:
            if self._ver_worker is not None:
                self._left_btn.setEnabled(False)
                self._ver_worker.cancel()
        elif self._step == Step.DRIVES:
            if self._prepared is not None and self._prepared.is_local:
                self._go_to(Step.SETUP)
            else:
                self._download_only = True
                self._go_to(Step.DONE)
        elif self._step == Step.WRITING:
            if confirm_stop_write_dialog(self):
                if self._write_worker is not None:
                    self._left_btn.setEnabled(False)
                    self._write_worker.request_cancel()

    def _on_mid_action(self) -> None:
        if self._step in (Step.WELCOME, Step.SETUP):
            self._choose_local_image()
        elif self._step == Step.DOWNLOADING and self._dl_worker is not None:
            dl = self._dl_worker.downloader
            if dl.is_paused:
                dl.resume()
                self._headline.setText("Downloading")
                self._mid_btn.setText("Pause")
            else:
                dl.pause()
                self._headline.setText("Paused")
                self._mid_btn.setText("Resume")
        elif self._step == Step.DONE and self._archive_path is not None:
            QtGui.QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._archive_path.parent)))

    def _on_primary_action(self) -> None:
        if self._step == Step.WELCOME:
            self._go_to(Step.SETUP)
            return

        if self._step == Step.SETUP:
            rel = self.setup_page.current_release()
            if rel is None:
                self.setup_page.banner.show_message("")
                self._start_catalog_fetch()
                self._refresh_setup_state()
                return
            self._active_release = rel
            self.setup_page.banner.show_message("")
            dest = self.setup_page.path_selector.path
            dl_state, _ = inspect_existing_download(dest, rel, allow_aria2=self._use_aria2)
            if dl_state == DownloadState.COMPLETE:
                self._start_verification(dest / rel.archive_name)
            else:
                self._start_download(rel, dest)
            return

        if self._step == Step.DRIVES:
            drv = self.drive_page.selected_drive()
            if drv is None or self._prepared is None:
                return
            mode = self.drive_page.install_mode()
            if confirm_write_dialog(self, self._prepared, drv, install_mode=mode):
                self._target_drive = drv
                self._start_write(drv)
            return

        if self._step == Step.DONE:
            self.close()

    def _start_download(self, release: Release, dest_dir: Path) -> None:
        self.progress_page.reset_indeterminate()
        self._go_to(Step.DOWNLOADING)

        worker = DownloadWorker(release, dest_dir, use_aria2=self._use_aria2)
        self._dl_worker = worker
        worker.status_changed.connect(self._on_dl_status)
        worker.progress.connect(self._on_dl_progress)
        worker.completed.connect(self._on_dl_completed)
        worker.cancelled.connect(self._on_dl_cancelled)
        worker.failed.connect(self._on_dl_failed)
        self._launch_worker(worker)

    def _on_dl_status(self, msg: str) -> None:
        if self._step == Step.DOWNLOADING:
            self._headline.setText(msg or "Downloading")

    def _on_dl_progress(self, done: int, total: int, bps: int) -> None:
        self.progress_page.update_metrics(done, total, bps)

    def _on_dl_completed(self, archive_path: Path) -> None:
        self._dl_worker = None
        self._start_verification(archive_path)

    def _on_dl_cancelled(self) -> None:
        self._dl_worker = None
        self._left_btn.setEnabled(True)
        self._go_to(Step.SETUP)

    def _on_dl_failed(self, err: str) -> None:
        self._dl_worker = None
        self._left_btn.setEnabled(True)
        self.setup_page.banner.show_message(err, kind="error")
        self._go_to(Step.SETUP)

    def _start_verification(self, archive_path: Path) -> None:
        assert self._active_release is not None
        self._archive_path = archive_path
        self._verifying_local = False
        self._verifying_label = self._release_tag()
        self.progress_page.reset_indeterminate()
        self._go_to(Step.VERIFYING)

        worker = VerifyWorker(archive_path, self._active_release)
        self._ver_worker = worker
        worker.progress.connect(self._on_verify_progress)
        worker.completed.connect(self._on_verify_completed)
        worker.cancelled.connect(self._on_verify_cancelled)
        worker.failed.connect(self._on_verify_failed)
        self._launch_worker(worker)

    def _start_local_verification(self, local_path: Path) -> None:
        self._archive_path = local_path
        self._verifying_local = True
        self._verifying_label = local_path.name
        self.progress_page.reset_indeterminate()
        self._go_to(Step.VERIFYING)

        worker = VerifyWorker(local_path, None, catalog=self._releases)
        self._ver_worker = worker
        worker.progress.connect(self._on_verify_progress)
        worker.completed.connect(self._on_verify_completed)
        worker.cancelled.connect(self._on_verify_cancelled)
        worker.failed.connect(self._on_verify_failed)
        self._launch_worker(worker)

    def _on_verify_progress(self, done: int, total: int, bps: int) -> None:
        self.progress_page.update_metrics(done, total, bps)

    def _on_verify_completed(self, prepared: PreparedImage) -> None:
        self._ver_worker = None
        self._prepared = prepared
        self._archive_path = prepared.source_path
        self.drive_page.banner.show_message("")
        self._go_to(Step.DRIVES)

    def _on_verify_cancelled(self) -> None:
        self._ver_worker = None
        self._left_btn.setEnabled(True)
        self._go_to(Step.SETUP)

    def _on_verify_failed(self, err: str) -> None:
        self._ver_worker = None
        self._left_btn.setEnabled(True)
        if not self._verifying_local and self._archive_path is not None:
            self._archive_path.unlink(missing_ok=True)
        self._archive_path = None
        self._prepared = None
        self.setup_page.banner.show_message(err, kind="error")
        self._go_to(Step.SETUP)

    def _trigger_drive_scan(self) -> None:
        if self._step != Step.DRIVES:
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        min_sz = self._prepared.image_size if self._prepared else (
            self._active_release.image_size if self._active_release else 0
        )
        src_path = self._prepared.source_path if self._prepared else self._archive_path
        is_local = bool(self._prepared and self._prepared.is_local)
        worker = DriveScanWorker(
            include_hdd=self.drive_page.hdd_checkbox.isChecked(),
            image_path=src_path,
            min_size=min_sz,
            is_local=is_local,
        )
        self._scan_worker = worker
        worker.scanned.connect(self._on_drives_scanned)
        self._launch_worker(worker)

    def _on_drives_scanned(self, entries: list[DriveEntry]) -> None:
        if self._step == Step.DRIVES:
            self.drive_page.set_entries(entries)

    def _start_write(self, drive: Drive) -> None:
        assert self._prepared is not None
        mode = self.drive_page.install_mode()
        opts = self.drive_page.boot_options()
        verify_drive = self.drive_page.verify_drive_checked()

        spec = WriteSpec(
            archive_path=self._prepared.source_path,
            member_name=self._prepared.member_name,
            device=drive.id,
            expected_serial=drive.serial,
            expected_size=drive.size,
            image_size=self._prepared.image_size,
            expected_sha1=self._prepared.sha1,
            verify_drive=verify_drive,
            install_mode=mode,
            cros_debug=opts.cros_debug,
            disable_auto_updates=opts.disable_auto_updates,
            verbose_boot=opts.verbose_boot,
            timeout=opts.timeout,
            nomodeset=opts.nomodeset,
            unencrypted_stateful=opts.unencrypted_stateful,
        )
        self.progress_page.reset_indeterminate()
        self._go_to(Step.WRITING)

        worker = WriteWorker(spec)
        self._write_worker = worker
        worker.password_needed.connect(self._on_password_needed)
        worker.progress.connect(self._on_write_progress)
        worker.completed.connect(self._on_write_completed)
        worker.cancelled.connect(self._on_write_cancelled)
        worker.failed.connect(self._on_write_failed)
        self._launch_worker(worker)

    def _on_password_needed(self, error_msg: str | None) -> None:
        if self._write_worker is None:
            return
        pw = password_dialog(self, error_msg)
        self._write_worker.provide_password(pw)

    def _on_write_progress(self, phase: str, done: int, total: int, bps: int) -> None:
        if self._step == Step.WRITING:
            drv_name = self._target_drive.name if self._target_drive else "USB drive"
            self._headline.setText("Verifying drive" if phase == "verify" else f"Writing to {drv_name}")
        self.progress_page.update_metrics(done, total, bps)

    def _on_write_completed(self) -> None:
        self._write_worker = None
        self._download_only = False
        self._go_to(Step.DONE)

    def _on_write_cancelled(self, started_writing: bool) -> None:
        self._write_worker = None
        self._left_btn.setEnabled(True)
        if started_writing:
            self.drive_page.banner.show_message(
                "Writing was stopped. Write again before using this drive.",
                kind="warning",
            )
        else:
            self.drive_page.banner.show_message("")
        self._go_to(Step.DRIVES)

    def _on_write_failed(self, err: str) -> None:
        self._write_worker = None
        self._left_btn.setEnabled(True)
        self.drive_page.banner.show_message(err, kind="error")
        self._go_to(Step.DRIVES)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._write_worker is not None and self._write_worker.isRunning():
            if self._step == Step.WRITING and not confirm_stop_write_dialog(self):
                event.ignore()
                return
            self._write_worker.request_cancel()

        if self._dl_worker is not None and self._dl_worker.isRunning():
            self._dl_worker.downloader.cancel()

        if self._ver_worker is not None and self._ver_worker.isRunning():
            self._ver_worker.cancel()

        for worker in list(self._active_threads):
            if worker.isRunning():
                worker.wait(5000)

        close_privileged_session()
        event.accept()


__all__ = ["FlexKitWindow", "QSize", "QtCore", "Step"]
