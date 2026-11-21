from __future__ import annotations

from pathlib import Path

from flexkit.catalog import Release, format_bytes
from flexkit.drives import Drive, DriveEntry
from flexkit.ui import theme
from flexkit.ui.qt import QPointF, QRectF, Qt, QtGui, QtWidgets, Signal
from flexkit.ui.widgets import (
    BadgeIcon,
    Banner,
    BootOptionTile,
    DriveCard,
    IconButton,
    LinearProgress,
    MaterialCheckbox,
    OutlinedDropdown,
    PathSelector,
    TextButton,
)


class SurfaceCard(QtWidgets.QFrame):
    def __init__(
        self,
        bg_hex: str = theme.SURFACE_LOW,
        border_hex: str | None = theme.DIVIDER,
        radius: float = 16.0,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._bg = bg_hex
        self._border = border_hex
        self._radius = radius

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self._border:
            p.setPen(QtGui.QPen(theme.color(self._border), 1.0))
        else:
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color(self._bg))
        p.drawRoundedRect(rect, self._radius, self._radius)
        p.end()


class OobeIllustration(QtWidgets.QWidget):
    def __init__(self, mode: str = "welcome", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = mode
        self.setMinimumSize(180, 160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_mode(self, mode: str) -> None:
        if self._mode != mode:
            self._mode = mode
            self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        cx, cy = w / 2.0, h / 2.0
        unit = min(w, h) * 0.48

        if self._mode == "done":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.SUCCESS_CONTAINER, 0.7))
            p.drawEllipse(QPointF(cx, cy), unit * 0.88, unit * 0.88)

            stick_w, stick_h = unit * 0.52, unit * 0.78
            sx, sy = cx - stick_w / 2.0, cy - stick_h * 0.42
            p.setBrush(theme.color("#CEEAD6"))
            p.drawRoundedRect(QRectF(sx + stick_w * 0.22, sy - stick_h * 0.24, stick_w * 0.56, stick_h * 0.28), 4.0, 4.0)

            p.setBrush(theme.color(theme.SURFACE))
            p.setPen(QtGui.QPen(theme.color(theme.SUCCESS), 2.2))
            p.drawRoundedRect(QRectF(sx, sy, stick_w, stick_h), 12.0, 12.0)

            badge_r = unit * 0.24
            bx, by = sx + stick_w, sy + stick_h * 0.78
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.SUCCESS))
            p.drawEllipse(QPointF(bx, by), badge_r, badge_r)
            theme.draw_glyph(
                p,
                "check",
                QRectF(bx - badge_r * 0.72, by - badge_r * 0.72, badge_r * 1.44, badge_r * 1.44),
                theme.SURFACE,
                stroke_width=2.4,
            )
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.SURFACE_CONTAINER))
            p.drawEllipse(QPointF(cx, cy), unit * 0.92, unit * 0.92)

            scr_w, scr_h = unit * 1.08, unit * 0.70
            sx, sy = cx - scr_w * 0.56, cy - scr_h * 0.52
            p.setBrush(theme.color(theme.SURFACE))
            p.setPen(QtGui.QPen(theme.color(theme.OUTLINE_VARIANT), 2.0))
            p.drawRoundedRect(QRectF(sx, sy, scr_w, scr_h), 8.0, 8.0)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.PRIMARY_CONTAINER))
            p.drawRoundedRect(QRectF(sx + 8, sy + 8, scr_w - 16, scr_h - 16), 4.0, 4.0)

            base_w = scr_w * 1.22
            p.setBrush(theme.color(theme.OUTLINE_VARIANT))
            p.drawRoundedRect(QRectF(sx - (base_w - scr_w) / 2.0, sy + scr_h + 2.0, base_w, 6.0), 3.0, 3.0)

            usb_w, usb_h = unit * 0.36, unit * 0.56
            ux, uy = sx + scr_w + unit * 0.12, cy - usb_h * 0.22
            p.setBrush(theme.color(theme.PRIMARY_CONTAINER))
            p.drawRoundedRect(QRectF(ux + usb_w * 0.22, uy - usb_h * 0.24, usb_w * 0.56, usb_h * 0.26), 3.0, 3.0)
            p.setBrush(theme.color(theme.PRIMARY))
            p.drawRoundedRect(QRectF(ux, uy, usb_w, usb_h), 8.0, 8.0)

        p.end()


class WelcomePage(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(OobeIllustration("welcome", self), 1)


class SetupPage(QtWidgets.QWidget):
    selection_changed = Signal()

    def __init__(self, initial_dir: Path, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)
        lay.addStretch(1)

        self.banner = Banner("error", self)
        lay.addWidget(self.banner)

        self.path_selector = PathSelector(initial_dir, self)
        self.path_selector.path_changed.connect(lambda _p: self.selection_changed.emit())
        lay.addWidget(self.path_selector)

        self.channel_dropdown = OutlinedDropdown("Channel", self)
        self.channel_dropdown.changed.connect(lambda _i: self.selection_changed.emit())
        lay.addWidget(self.channel_dropdown)

        self.summary_card = SurfaceCard(theme.SURFACE_LOW, theme.DIVIDER, radius=12.0, parent=self)
        s_lay = QtWidgets.QHBoxLayout(self.summary_card)
        s_lay.setContentsMargins(18, 13, 18, 13)
        self.ver_label = QtWidgets.QLabel("", self.summary_card)
        self.ver_label.setFont(theme.make_font(14, QtGui.QFont.Weight.Normal, tabular=True))
        self.ver_label.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
        self.size_label = QtWidgets.QLabel("", self.summary_card)
        self.size_label.setFont(theme.make_font(14, QtGui.QFont.Weight.Medium, tabular=True))
        self.size_label.setStyleSheet(f"color: {theme.ON_SURFACE}; background: transparent;")
        s_lay.addWidget(self.ver_label, 1)
        s_lay.addWidget(self.size_label, 0)
        self.summary_card.hide()
        lay.addWidget(self.summary_card)

        lay.addStretch(2)

    def current_release(self) -> Release | None:
        data = self.channel_dropdown.current_data()
        return data if isinstance(data, Release) else None

    def set_releases(self, releases: list[Release]) -> None:
        items = [(rel.channel.label, rel) for rel in releases]
        self.channel_dropdown.set_items(items, 0)

    def update_summary(self, release: Release | None) -> None:
        if release is None:
            self.ver_label.setText("")
            self.size_label.setText("")
            self.summary_card.hide()
            return
        self.ver_label.setText(f"Version {release.version}")
        self.size_label.setText(format_bytes(release.zip_size))
        self.summary_card.show()


def _format_eta(remaining_bytes: int, bps: int) -> str:
    if bps <= 0 or remaining_bytes <= 0:
        return ""
    secs = int(remaining_bytes / bps)
    if secs < 60:
        return f"{max(1, secs)} sec left"
    mins = (secs + 30) // 60
    if mins < 60:
        return f"{mins} min left"
    hrs = mins // 60
    rem_m = mins % 60
    return f"{hrs} hr {rem_m} min left"


class ProgressPage(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)

        card = SurfaceCard(theme.SURFACE_LOW, theme.DIVIDER, radius=16.0, parent=self)
        c_lay = QtWidgets.QVBoxLayout(card)
        c_lay.setContentsMargins(24, 22, 24, 22)
        c_lay.setSpacing(14)

        top_row = QtWidgets.QHBoxLayout()
        self.pct_label = QtWidgets.QLabel("", card)
        self.pct_label.setFont(theme.make_font(28, QtGui.QFont.Weight.Medium, tabular=True))
        self.pct_label.setStyleSheet(f"color: {theme.PRIMARY}; background: transparent;")

        self.speed_label = QtWidgets.QLabel("", card)
        self.speed_label.setFont(theme.make_font(14, QtGui.QFont.Weight.Normal, tabular=True))
        self.speed_label.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")

        top_row.addWidget(self.pct_label, 0)
        top_row.addStretch(1)
        top_row.addWidget(self.speed_label, 0, Qt.AlignmentFlag.AlignBottom)
        c_lay.addLayout(top_row)

        self.bar = LinearProgress(card)
        c_lay.addWidget(self.bar)

        self.detail_label = QtWidgets.QLabel("", card)
        self.detail_label.setFont(theme.make_font(14, QtGui.QFont.Weight.Normal, tabular=True))
        self.detail_label.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
        c_lay.addWidget(self.detail_label)

        lay.addWidget(card)
        lay.addStretch(1)

    def reset_indeterminate(self, detail_text: str = "") -> None:
        self.pct_label.setText("")
        self.speed_label.setText("")
        self.bar.set_indeterminate(True)
        self.detail_label.setText(detail_text)

    def update_metrics(self, done: int, total: int, bps: int = 0) -> None:
        if total > 0:
            frac = max(0.0, min(1.0, done / total))
            self.bar.set_progress(frac)
            self.pct_label.setText(f"{int(frac * 100)}%")
            self.detail_label.setText(f"{format_bytes(done)} of {format_bytes(total)}")
            speed_parts: list[str] = []
            if bps > 0 and done < total:
                speed_parts.append(f"{format_bytes(bps)}/s")
                eta = _format_eta(total - done, bps)
                if eta:
                    speed_parts.append(eta)
            self.speed_label.setText(" · ".join(speed_parts))
        else:
            self.bar.set_indeterminate(True)
            self.pct_label.setText("")
            self.speed_label.setText("")
            self.detail_label.setText(format_bytes(done) if done > 0 else "")


class DrivePage(QtWidgets.QWidget):
    selection_changed = Signal()
    filter_toggled = Signal(bool)
    refresh_requested = Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[DriveEntry] = []
        self._cards: list[DriveCard] = []
        self._selected_id: str | None = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.banner = Banner("error", self)
        lay.addWidget(self.banner)

        self.stack = QtWidgets.QStackedWidget(self)

        empty_card = SurfaceCard(theme.SURFACE_LOW, theme.DIVIDER, radius=16.0, parent=self)
        e_lay = QtWidgets.QVBoxLayout(empty_card)
        e_lay.setContentsMargins(24, 32, 24, 32)
        e_lay.setSpacing(12)
        empty_icon = BadgeIcon(
            "usb",
            bg_hex=theme.SURFACE_CONTAINER_HIGH,
            fg_hex=theme.ON_SURFACE_VARIANT,
            box_size=52,
            parent=empty_card,
        )
        empty_title = QtWidgets.QLabel("Insert a USB drive", empty_card)
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setFont(theme.make_font(16, QtGui.QFont.Weight.Normal))
        empty_title.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
        e_lay.addStretch(1)
        e_lay.addWidget(empty_icon, 0, Qt.AlignmentFlag.AlignCenter)
        e_lay.addWidget(empty_title)
        e_lay.addStretch(1)
        self.stack.addWidget(empty_card)

        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_host = QtWidgets.QWidget(self.scroll)
        self._list_layout = QtWidgets.QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(10)

        self._drives_box = QtWidgets.QWidget(self._list_host)
        self._drives_layout = QtWidgets.QVBoxLayout(self._drives_box)
        self._drives_layout.setContentsMargins(0, 0, 0, 0)
        self._drives_layout.setSpacing(8)
        self._list_layout.addWidget(self._drives_box)

        self.options_card = SurfaceCard(theme.SURFACE_LOW, theme.DIVIDER, radius=12.0, parent=self._list_host)
        opt_lay = QtWidgets.QVBoxLayout(self.options_card)
        opt_lay.setContentsMargins(14, 12, 14, 12)
        opt_lay.setSpacing(10)

        self.mode_dropdown = OutlinedDropdown("Target Mode", self.options_card)
        self.mode_dropdown.setFixedHeight(54)
        self.mode_dropdown.set_items([
            ("USB Installer (Standard)", "installer"),
            ("ChromeOS To Go (Run from USB)", "to_go"),
        ])
        self.mode_dropdown.changed.connect(self._on_mode_changed)
        opt_lay.addWidget(self.mode_dropdown)

        self.mode_desc = QtWidgets.QLabel(
            "Creates a standard ChromeOS Flex USB installer to try or install on a PC's internal drive.",
            self.options_card,
        )
        self.mode_desc.setWordWrap(True)
        self.mode_desc.setFont(theme.make_font(12, QtGui.QFont.Weight.Normal))
        self.mode_desc.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent; padding: 0 2px;")
        opt_lay.addWidget(self.mode_desc)

        self.keep_data_tile = BootOptionTile(
            "Keep existing user data (Update OS only)",
            "Preserves user accounts, local files, and apps in the stateful partition; updates OS partitions only.",
            checked=True,
            parent=self.options_card,
        )
        self.keep_data_tile.setVisible(False)
        self.keep_data_tile.toggled.connect(lambda _checked: self.selection_changed.emit())
        opt_lay.addWidget(self.keep_data_tile)
        self.keep_data_cb = self.keep_data_tile

        self.adv_btn = TextButton("▸ Advanced Boot Options", self.options_card)
        self.adv_btn.setFixedHeight(28)
        self.adv_btn.clicked.connect(self._toggle_advanced_options)
        opt_lay.addWidget(self.adv_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self.adv_container = QtWidgets.QWidget(self.options_card)
        adv_lay = QtWidgets.QVBoxLayout(self.adv_container)
        adv_lay.setContentsMargins(0, 4, 0, 4)
        adv_lay.setSpacing(6)

        self.flexshim_tile = BootOptionTile(
            "Fix UEFI Boot Panic on Older PCs (flexshim)",
            "Installs the flexshim UEFI compatibility shim on the EFI partition to fix crdyboot revocation errors and 'RefCell already borrowed' panics on early UEFI 2.0 PCs (e.g., 2nd Gen Intel). Requires Secure Boot disabled.",
            parent=self.adv_container,
        )
        self.flexshim_tile.toggled.connect(self._on_flexshim_toggled)
        adv_lay.addWidget(self.flexshim_tile)

        self.block_updates_tile = BootOptionTile(
            "Disable Automatic Updates",
            "Sets block_updates=true in flexshim.cfg to block background ChromeOS Flex OTA updates without modifying OS partitions (requires flexshim; also enables crosh shell/VT2).",
            parent=self.adv_container,
        )
        self.block_updates_tile.toggled.connect(self._on_block_updates_toggled)
        adv_lay.addWidget(self.block_updates_tile)

        self.adv_container.setVisible(False)
        opt_lay.addWidget(self.adv_container)

        self._list_layout.addWidget(self.options_card)
        self._list_layout.addStretch(1)

        self.scroll.setWidget(self._list_host)
        self.stack.addWidget(self.scroll)

        lay.addWidget(self.stack, 1)

        ctrl_row = QtWidgets.QHBoxLayout()
        ctrl_row.setContentsMargins(0, 2, 0, 0)
        ctrl_row.setSpacing(12)
        self.verify_checkbox = MaterialCheckbox("Verify drive", self)
        self.verify_checkbox.setToolTip("Read back written disk sectors and verify checksum")
        self.verify_checkbox.setChecked(True)
        self.hdd_checkbox = MaterialCheckbox("Show hard drives", self)
        self.hdd_checkbox.setToolTip("Show external USB hard disk drives alongside flash sticks")
        self.hdd_checkbox.toggled.connect(self.filter_toggled.emit)
        self.refresh_btn = IconButton("refresh", "Refresh", self)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)

        ctrl_row.addWidget(self.verify_checkbox, 0)
        ctrl_row.addWidget(self.hdd_checkbox, 0)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self.refresh_btn, 0)
        lay.addLayout(ctrl_row)

    def _on_mode_changed(self, _idx: int) -> None:
        drv = self.selected_drive()
        has_cros = drv is not None and drv.has_chromeos
        is_togo = self.mode_dropdown.current_data() == "to_go"
        self.mode_desc.setText(
            "Installs persistent ChromeOS Flex directly onto the USB drive with Verified Boot and full storage (16 GB+ recommended)."
            if is_togo
            else "Creates a standard ChromeOS Flex USB installer to try or install on a PC's internal drive."
        )
        self.keep_data_tile.setVisible(has_cros and is_togo)
        self.selection_changed.emit()

    def _toggle_advanced_options(self) -> None:
        visible = not self.adv_container.isVisible()
        self.adv_container.setVisible(visible)
        self.adv_btn.setText("▾ Advanced Boot Options" if visible else "▸ Advanced Boot Options")

    def _on_flexshim_toggled(self, checked: bool) -> None:
        if not checked and self.block_updates_tile.isChecked():
            self.block_updates_tile.blockSignals(True)
            self.block_updates_tile.setChecked(False)
            self.block_updates_tile.blockSignals(False)
        self.selection_changed.emit()

    def _on_block_updates_toggled(self, checked: bool) -> None:
        if checked and not self.flexshim_tile.isChecked():
            self.flexshim_tile.blockSignals(True)
            self.flexshim_tile.setChecked(True)
            self.flexshim_tile.blockSignals(False)
        self.selection_changed.emit()

    def install_flexshim_checked(self) -> bool:
        return self.flexshim_tile.isChecked() or self.block_updates_tile.isChecked()

    def block_updates_checked(self) -> bool:
        return self.block_updates_tile.isChecked()

    def install_mode(self) -> str:
        data = self.mode_dropdown.current_data()
        if data == "to_go":
            if self.keep_data_tile.isVisible() and self.keep_data_tile.isChecked():
                return "to_go_update"
            return "to_go_fresh"
        return "installer"

    def verify_drive_checked(self) -> bool:
        return self.verify_checkbox.isChecked()

    def selected_drive(self) -> Drive | None:
        for entry in self._entries:
            if entry.selectable and entry.drive.id == self._selected_id:
                return entry.drive
        return None

    def set_entries(self, entries: list[DriveEntry]) -> None:
        if entries == self._entries:
            return
        self._entries = list(entries)

        while self._drives_layout.count() > 0:
            item = self._drives_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cards.clear()

        selectable_ids = {e.drive.id for e in entries if e.selectable}
        if self._selected_id not in selectable_ids:
            self._selected_id = next((e.drive.id for e in entries if e.selectable), None)

        if not entries:
            self.stack.setCurrentIndex(0)
            self.options_card.setVisible(False)
            self.selection_changed.emit()
            return

        self.options_card.setVisible(True)
        for idx, entry in enumerate(entries):
            is_sel = entry.selectable and entry.drive.id == self._selected_id
            card = DriveCard(entry, selected=is_sel, parent=self._drives_box)
            dev_id = entry.drive.id
            card.clicked.connect(lambda d=dev_id: self._select(d))
            self._drives_layout.addWidget(card)
            self._cards.append(card)

        self.stack.setCurrentIndex(1)
        if self._selected_id:
            self._select(self._selected_id)
        else:
            self.selection_changed.emit()

    def _select(self, dev_id: str) -> None:
        self._selected_id = dev_id
        for card in self._cards:
            card.set_selected(card.entry.selectable and card.entry.drive.id == dev_id)

        entry = next((e for e in self._entries if e.drive.id == dev_id), None)
        if entry and entry.drive.has_chromeos:
            self.mode_dropdown.set_current_index(1)
            self.keep_data_tile.setVisible(True)
            self.keep_data_tile.setChecked(True)
        else:
            self.mode_dropdown.set_current_index(0)
            self.keep_data_tile.setVisible(False)
            self.keep_data_tile.setChecked(False)

        self.selection_changed.emit()


class DonePage(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(OobeIllustration("done", self), 1)
