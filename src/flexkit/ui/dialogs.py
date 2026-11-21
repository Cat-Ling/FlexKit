from __future__ import annotations

from flexkit.catalog import Release, format_bytes
from flexkit.download import PreparedImage
from flexkit.drives import Drive
from flexkit.ui import theme
from flexkit.ui.qt import QRectF, QTimer, Qt, QtCore, QtGui, QtWidgets
from flexkit.ui.widgets import BadgeIcon, Banner, DestructiveButton, FilledButton, TextButton


class _CardFrame(QtWidgets.QFrame):
    def __init__(self, bg_hex: str, radius: float = 28.0, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._bg = bg_hex
        self._radius = radius

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color(self._bg))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), self._radius, self._radius)
        p.end()


class ScrimSheet(QtWidgets.QWidget):
    def __init__(self, host: QtWidgets.QWidget) -> None:
        super().__init__(host)
        self._host = host
        self._accepted = False
        self._loop: QtCore.QEventLoop | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        host.installEventFilter(self)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addStretch(1)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.card = _CardFrame(theme.SURFACE, radius=24.0, parent=self)
        self.card.setMinimumWidth(320)
        max_w = min(540, max(340, host.width() - 32))
        self.card.setMaximumWidth(max_w)
        self.card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Minimum)
        row.addWidget(self.card, 1)
        row.addStretch(1)

        outer.addLayout(row)
        outer.addStretch(1)

        self.card_layout = QtWidgets.QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(22, 18, 22, 16)
        self.card_layout.setSpacing(10)

        self.setGeometry(host.rect())

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._host and event.type() == QtCore.QEvent.Type.Resize:
            self.setGeometry(self._host.rect())
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def accept(self) -> None:
        self._accepted = True
        self._finish()

    def reject(self) -> None:
        self._accepted = False
        self._finish()

    def _finish(self) -> None:
        if self._loop is not None and self._loop.isRunning():
            self._loop.quit()

    def exec_sheet(self) -> bool:
        self.setGeometry(self._host.rect())
        self.raise_()
        self.show()
        self.setFocus()
        loop = QtCore.QEventLoop(self)
        self._loop = loop
        loop.exec()
        self._host.removeEventFilter(self)
        self.hide()
        self.deleteLater()
        return self._accepted

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), theme.color(theme.ON_SURFACE, 0.36))
        p.end()


def confirm_write_dialog(
    host: QtWidgets.QWidget,
    image: PreparedImage | Release,
    drive: Drive,
    install_mode: str = "installer",
    *,
    install_flexshim: bool = False,
    block_updates: bool = False,
) -> bool:
    sheet = ScrimSheet(host)
    lay = sheet.card_layout

    is_update = install_mode == "to_go_update"

    if is_update:
        icon = BadgeIcon("flex", bg_hex="#D2E3FC", fg_hex=theme.PRIMARY, box_size=40, parent=sheet.card)
        title_text = "Update ChromeOS Flex?"
    else:
        icon = BadgeIcon("warning", bg_hex=theme.ERROR_CONTAINER, fg_hex=theme.ERROR, box_size=40, parent=sheet.card)
        title_text = "Erase and write drive?"

    lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignLeft)

    title = QtWidgets.QLabel(title_text, sheet.card)
    title.setFont(theme.make_font(21, QtGui.QFont.Weight.Normal))
    lay.addWidget(title)

    info_box = _CardFrame(theme.SURFACE_CONTAINER, radius=12.0, parent=sheet.card)
    info_grid = QtWidgets.QGridLayout(info_box)
    info_grid.setContentsMargins(14, 10, 14, 10)
    info_grid.setHorizontalSpacing(14)
    info_grid.setVerticalSpacing(4)

    def add_row(r: int, k: str, v: str) -> None:
        lbl_k = QtWidgets.QLabel(k, info_box)
        lbl_k.setFont(theme.make_font(12, QtGui.QFont.Weight.Medium))
        lbl_k.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
        lbl_v = QtWidgets.QLabel(v, info_box)
        lbl_v.setFont(theme.make_font(13, QtGui.QFont.Weight.Normal))
        lbl_v.setStyleSheet("background: transparent;")
        lbl_v.setWordWrap(True)
        info_grid.addWidget(lbl_k, r, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_grid.addWidget(lbl_v, r, 1)

    drive_desc = f"{drive.name} · {format_bytes(drive.size)} ({drive.id})"
    add_row(0, "Target", drive_desc)

    if is_update:
        mode_desc = "ChromeOS To Go (Keep user data, update OS)"
    elif install_mode == "to_go_fresh":
        mode_desc = "ChromeOS To Go (Full drive install)"
    else:
        mode_desc = "USB Recovery Installer"
    add_row(1, "Mode", mode_desc)

    if isinstance(image, PreparedImage):
        img_desc = f"{image.title} · {format_bytes(image.image_size)}"
    else:
        img_desc = f"ChromeOS Flex {image.version} ({image.channel.label})"
    add_row(2, "Image", img_desc)

    if install_flexshim or block_updates:
        shim_desc = (
            "flexshim (UEFI panic fix) · Automatic updates disabled"
            if block_updates
            else "flexshim (UEFI panic fix)"
        )
        add_row(3, "Boot shim", shim_desc)

    info_grid.setColumnStretch(1, 1)
    lay.addWidget(info_box)

    if drive.is_usb_hard_drive:
        hdd_warn = Banner("warning", sheet.card)
        hdd_warn.show_message("This is a USB hard drive, not a flash drive.")
        lay.addWidget(hdd_warn)

    if install_flexshim or block_updates:
        shim_warn = Banner("warning", sheet.card)
        shim_warn.show_message("flexshim is unsigned — disable UEFI Secure Boot on the target PC before booting.")
        lay.addWidget(shim_warn)

    if is_update:
        warn_txt = QtWidgets.QLabel(
            "User accounts and files in the STATE partition will remain completely safe. Only OS partitions will be updated.",
            sheet.card,
        )
    else:
        warn_txt = QtWidgets.QLabel(
            "All existing data and partitions on this drive will be permanently erased.",
            sheet.card,
        )
    warn_txt.setWordWrap(True)
    warn_txt.setFont(theme.make_font(13, QtGui.QFont.Weight.Normal))
    warn_txt.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
    lay.addWidget(warn_txt)

    btn_row = QtWidgets.QHBoxLayout()
    btn_row.addStretch(1)
    cancel_btn = TextButton("Cancel", sheet.card)
    cancel_btn.clicked.connect(sheet.reject)
    if is_update:
        confirm_btn = FilledButton("Update OS", sheet.card)
    else:
        confirm_btn = DestructiveButton("Erase and write", sheet.card)
    confirm_btn.clicked.connect(sheet.accept)
    btn_row.addWidget(cancel_btn)
    btn_row.addWidget(confirm_btn)
    lay.addLayout(btn_row)

    if drive.is_usb_hard_drive and not is_update:
        remaining = [2]
        confirm_btn.setEnabled(False)
        confirm_btn.setText("Erase and write (2)")
        timer = QTimer(sheet)
        timer.setInterval(1000)

        def tick() -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                timer.stop()
                confirm_btn.setText("Erase and write")
                confirm_btn.setEnabled(True)
            else:
                confirm_btn.setText(f"Erase and write ({remaining[0]})")

        timer.timeout.connect(tick)
        timer.start()

    return sheet.exec_sheet()


def password_dialog(host: QtWidgets.QWidget, error_message: str | None = None) -> bytearray | None:
    sheet = ScrimSheet(host)
    lay = sheet.card_layout

    icon = BadgeIcon("lock", bg_hex=theme.PRIMARY_CONTAINER, fg_hex=theme.PRIMARY, box_size=48, parent=sheet.card)
    lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignLeft)

    title = QtWidgets.QLabel("Authentication required", sheet.card)
    title.setFont(theme.make_font(24, QtGui.QFont.Weight.Normal))
    lay.addWidget(title)

    sub = QtWidgets.QLabel("Enter your password to write to the USB drive.", sheet.card)
    sub.setWordWrap(True)
    sub.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
    sub.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT}; background: transparent;")
    lay.addWidget(sub)

    field_box = QtWidgets.QHBoxLayout()
    field_box.setSpacing(6)
    pw_edit = QtWidgets.QLineEdit(sheet.card)
    pw_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
    pw_edit.setPlaceholderText("Password")
    pw_edit.setFixedHeight(48)
    pw_edit.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
    border_col = theme.ERROR if error_message else theme.OUTLINE
    pw_edit.setStyleSheet(
        f"""
        QLineEdit {{
            background: {theme.SURFACE};
            border: 1.5px solid {border_col};
            border-radius: 8px;
            padding: 0 12px;
            color: {theme.ON_SURFACE};
        }}
        QLineEdit:focus {{
            border: 2px solid {theme.PRIMARY};
        }}
        """
    )
    toggle_btn = TextButton("Show", sheet.card)

    def toggle_echo() -> None:
        hidden = pw_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        pw_edit.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if hidden else QtWidgets.QLineEdit.EchoMode.Password
        )
        toggle_btn.setText("Hide" if hidden else "Show")

    toggle_btn.clicked.connect(toggle_echo)
    field_box.addWidget(pw_edit, 1)
    field_box.addWidget(toggle_btn)
    lay.addLayout(field_box)

    if error_message:
        err_lbl = QtWidgets.QLabel(error_message, sheet.card)
        err_lbl.setFont(theme.make_font(12, QtGui.QFont.Weight.Medium))
        err_lbl.setStyleSheet(f"color: {theme.ERROR};")
        lay.addWidget(err_lbl)

    btn_row = QtWidgets.QHBoxLayout()
    btn_row.addStretch(1)
    cancel_btn = TextButton("Cancel", sheet.card)
    cancel_btn.clicked.connect(sheet.reject)
    ok_btn = FilledButton("Continue", sheet.card)
    ok_btn.clicked.connect(sheet.accept)
    pw_edit.returnPressed.connect(sheet.accept)
    btn_row.addWidget(cancel_btn)
    btn_row.addWidget(ok_btn)
    lay.addLayout(btn_row)

    QTimer.singleShot(0, pw_edit.setFocus)
    accepted = sheet.exec_sheet()
    if not accepted:
        pw_edit.clear()
        return None
    raw = bytearray(pw_edit.text().encode("utf-8"))
    pw_edit.clear()
    return raw


def confirm_stop_write_dialog(host: QtWidgets.QWidget) -> bool:
    sheet = ScrimSheet(host)
    lay = sheet.card_layout

    icon = BadgeIcon("warning", bg_hex=theme.ERROR_CONTAINER, fg_hex=theme.ERROR, box_size=44, parent=sheet.card)
    lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignLeft)

    title = QtWidgets.QLabel("Stop writing?", sheet.card)
    title.setFont(theme.make_font(22, QtGui.QFont.Weight.Normal))
    lay.addWidget(title)

    body = QtWidgets.QLabel("The USB drive will be unusable until written again.", sheet.card)
    body.setWordWrap(True)
    body.setFont(theme.make_font(14, QtGui.QFont.Weight.Normal))
    body.setStyleSheet(f"color: {theme.ON_SURFACE_VARIANT};")
    lay.addWidget(body)

    btn_row = QtWidgets.QHBoxLayout()
    btn_row.addStretch(1)
    keep_btn = TextButton("Continue writing", sheet.card)
    keep_btn.clicked.connect(sheet.reject)
    stop_btn = DestructiveButton("Stop", sheet.card)
    stop_btn.clicked.connect(sheet.accept)
    btn_row.addWidget(keep_btn)
    btn_row.addWidget(stop_btn)
    lay.addLayout(btn_row)

    return sheet.exec_sheet()
