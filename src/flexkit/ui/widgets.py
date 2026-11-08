from __future__ import annotations

from pathlib import Path

from flexkit.drives import DriveEntry
from flexkit.ui import theme
from flexkit.ui.qt import QPoint, QPointF, QRectF, QSize, QTimer, Qt, QtGui, QtWidgets, Signal


class BadgeIcon(QtWidgets.QWidget):
    def __init__(
        self,
        glyph: str = "flex",
        *,
        bg_hex: str = theme.PRIMARY_CONTAINER,
        fg_hex: str = theme.PRIMARY,
        box_size: int = 56,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._bg = bg_hex
        self._fg = fg_hex
        self.setFixedSize(box_size, box_size)

    def set_box_size(self, box_size: int) -> None:
        if self.width() != box_size or self.height() != box_size:
            self.setFixedSize(box_size, box_size)
            self.update()

    def set_spec(self, glyph: str, bg_hex: str, fg_hex: str) -> None:
        self._glyph = glyph
        self._bg = bg_hex
        self._fg = fg_hex
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color(self._bg))
        w, h = float(self.width()), float(self.height())
        radius = min(w, h) * 0.30
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
        pad = min(w, h) * 0.21
        theme.draw_glyph(p, self._glyph, QRectF(pad, pad, w - 2 * pad, h - 2 * pad), self._fg, stroke_width=2.1)
        p.end()


class MaterialButton(QtWidgets.QPushButton):
    def __init__(
        self,
        text: str = "",
        *,
        variant: str = "filled",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._variant = variant
        self._hovered = False
        self._kbd_focus = False
        self.setFixedHeight(44)
        self.setFont(theme.make_font(15, QtGui.QFont.Weight.Medium))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def set_variant(self, variant: str) -> None:
        self._variant = variant
        self.update()

    def sizeHint(self) -> QSize:
        fm = QtGui.QFontMetrics(self.font())
        pad = 24 if self._variant == "text" else 42
        min_w = 64 if self._variant == "text" else 88
        w = max(min_w, fm.horizontalAdvance(self.text()) + pad)
        return QSize(w, 44)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtGui.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        r = rect.height() / 2.0

        enabled = self.isEnabled()
        pressed = self.isDown()

        bg: QtGui.QColor | None = None
        fg: QtGui.QColor
        border: QtGui.QColor | None = None
        overlay_base: str = theme.ON_PRIMARY

        if not enabled:
            if self._variant in ("filled", "destructive", "tonal"):
                bg = theme.color(theme.ON_SURFACE, 0.12)
            elif self._variant == "outlined":
                border = theme.color(theme.ON_SURFACE, 0.12)
            fg = theme.color(theme.ON_SURFACE, 0.38)
        elif self._variant == "filled":
            bg = theme.color(theme.PRIMARY)
            fg = theme.color(theme.ON_PRIMARY)
            overlay_base = theme.ON_PRIMARY
        elif self._variant == "destructive":
            bg = theme.color(theme.ERROR)
            fg = theme.color(theme.ON_ERROR)
            overlay_base = theme.ON_ERROR
        elif self._variant == "tonal":
            bg = theme.color(theme.PRIMARY_CONTAINER)
            fg = theme.color(theme.ON_PRIMARY_CONTAINER)
            overlay_base = theme.ON_PRIMARY_CONTAINER
        elif self._variant == "outlined":
            fg = theme.color(theme.PRIMARY)
            border = theme.color(theme.OUTLINE)
            overlay_base = theme.PRIMARY
        else:
            fg = theme.color(theme.PRIMARY)
            overlay_base = theme.PRIMARY

        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(rect, r, r)

        if enabled and (pressed or self._hovered):
            alpha = 0.12 if pressed else 0.08
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(overlay_base, alpha))
            p.drawRoundedRect(rect, r, r)

        if border is not None:
            p.setPen(QtGui.QPen(border, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, r, r)

        if enabled and self.hasFocus() and self._kbd_focus:
            focus_pen = QtGui.QPen(theme.color(theme.PRIMARY), 2.0)
            p.setPen(focus_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0), r - 1.0, r - 1.0)

        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class FilledButton(MaterialButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, variant="filled", parent=parent)


class TonalButton(MaterialButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, variant="tonal", parent=parent)


class OutlinedButton(MaterialButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, variant="outlined", parent=parent)


class TextButton(MaterialButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, variant="text", parent=parent)


class DestructiveButton(MaterialButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, variant="destructive", parent=parent)


class IconButton(QtWidgets.QPushButton):
    def __init__(
        self,
        glyph: str,
        tooltip: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._hovered = False
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
            self.setAccessibleName(tooltip)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtGui.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if self.isEnabled() and (self.isDown() or self._hovered):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.ON_SURFACE_VARIANT, 0.12 if self.isDown() else 0.08))
            p.drawEllipse(rect)
        fg = theme.ON_SURFACE_VARIANT if self.isEnabled() else theme.OUTLINE_VARIANT
        theme.draw_glyph(p, self._glyph, QRectF(9, 9, 22, 22), fg, stroke_width=1.9)
        p.end()


class LinearProgress(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._indeterminate = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.setFixedHeight(8)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def set_progress(self, fraction: float) -> None:
        if self._indeterminate:
            self._indeterminate = False
            self._timer.stop()
        self._value = max(0.0, min(1.0, fraction))
        self.update()

    def set_indeterminate(self, active: bool) -> None:
        if self._indeterminate == active:
            return
        self._indeterminate = active
        if active:
            self._phase = 0.0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.016) % 1.0
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        r = h / 2.0

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color(theme.PRIMARY_CONTAINER))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)

        p.setBrush(theme.color(theme.PRIMARY))
        if self._indeterminate:
            seg_w = w * 0.34
            start = (w + seg_w) * self._phase - seg_w
            left = max(0.0, start)
            right = min(w, start + seg_w)
            if right > left:
                p.drawRoundedRect(QRectF(left, 0, right - left, h), r, r)
        elif self._value > 0.0:
            fill_w = max(h, w * self._value)
            p.drawRoundedRect(QRectF(0, 0, min(w, fill_w), h), r, r)
        p.end()


class MaterialCheckbox(QtWidgets.QAbstractButton):
    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self._hovered = False
        self.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)

    def sizeHint(self) -> QSize:
        fm = QtGui.QFontMetrics(self.font())
        return QSize(38 + fm.horizontalAdvance(self.text()) + 8, 38)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtGui.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2.0
        box = QRectF(7, cy - 10, 20, 20)

        if self.isEnabled() and (self._hovered or self.hasFocus()):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.PRIMARY if self.isChecked() else theme.ON_SURFACE, 0.08))
            p.drawEllipse(QPointF(17, cy), 17, 17)

        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.PRIMARY if self.isEnabled() else theme.OUTLINE_VARIANT))
            p.drawRoundedRect(box, 4.0, 4.0)
            theme.draw_glyph(p, "check", box.adjusted(1.5, 1.5, -1.5, -1.5), theme.ON_PRIMARY, stroke_width=2.2)
        else:
            p.setPen(QtGui.QPen(theme.color(theme.ON_SURFACE_VARIANT), 1.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box, 4.0, 4.0)

        p.setPen(theme.color(theme.ON_SURFACE if self.isEnabled() else theme.OUTLINE))
        p.setFont(self.font())
        text_rect = QRectF(36, 0, max(0.0, self.width() - 36.0), float(self.height()))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


class OutlinedDropdown(QtWidgets.QWidget):
    changed = Signal(int)

    def __init__(self, label: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._items: list[tuple[str, object]] = []
        self._index = 0
        self._hovered = False
        self._open = False
        self.setFixedHeight(62)
        self.setMinimumWidth(140)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(label)

    def set_items(self, items: list[tuple[str, object]], current_index: int = 0) -> None:
        self._items = list(items)
        self._index = max(0, min(current_index, max(0, len(self._items) - 1)))
        self.update()

    def current_data(self) -> object | None:
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None

    def set_current_index(self, idx: int) -> None:
        if 0 <= idx < len(self._items) and idx != self._index:
            self._index = idx
            self.update()
            self.changed.emit(idx)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtGui.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled() and self._items:
            self._show_popup()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
            if self._items:
                self._show_popup()
                return
        super().keyPressEvent(event)

    def _show_popup(self) -> None:
        self._open = True
        self.update()
        menu = QtWidgets.QMenu(self)
        menu.setMinimumWidth(self.width())
        menu.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {theme.SURFACE_LOW};
                border: 1px solid {theme.OUTLINE_VARIANT};
                border-radius: 10px;
                padding: 6px 0px;
            }}
            QMenu::item {{
                padding: 10px 18px;
                color: {theme.ON_SURFACE};
            }}
            QMenu::item:selected {{
                background-color: {theme.PRIMARY_CONTAINER};
                color: {theme.ON_PRIMARY_CONTAINER};
            }}
            """
        )
        actions: list[QtGui.QAction] = []
        for idx, (txt, _) in enumerate(self._items):
            act = menu.addAction(txt)
            act.setData(idx)
            actions.append(act)
        chosen = menu.exec(self.mapToGlobal(QPoint(0, self.height() + 2)))
        self._open = False
        self.update()
        if chosen is not None:
            new_idx = int(chosen.data())
            if new_idx != self._index:
                self._index = new_idx
                self.changed.emit(new_idx)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        box = QRectF(1.0, 7.0, self.width() - 2.0, self.height() - 8.0)

        active = self._open or self.hasFocus()
        border_col = theme.PRIMARY if active else (theme.ON_SURFACE if self._hovered else theme.OUTLINE)
        border_w = 2.0 if active else 1.0

        p.setPen(QtGui.QPen(theme.color(border_col), border_w))
        p.setBrush(theme.color(theme.SURFACE))
        p.drawRoundedRect(box, 10.0, 10.0)

        lbl_font = theme.make_font(12, QtGui.QFont.Weight.Medium if active else QtGui.QFont.Weight.Normal)
        lbl_fm = QtGui.QFontMetrics(lbl_font)
        lbl_w = lbl_fm.horizontalAdvance(self._label) + 8
        p.fillRect(QRectF(14.0, 0.0, float(lbl_w), 15.0), theme.color(theme.SURFACE))
        p.setFont(lbl_font)
        p.setPen(theme.color(theme.PRIMARY if active else theme.ON_SURFACE_VARIANT))
        p.drawText(QRectF(18.0, 0.0, float(lbl_w), 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        val_text = self._items[self._index][0] if self._items else ""
        val_font = theme.make_font(15, QtGui.QFont.Weight.Normal)
        p.setFont(val_font)
        p.setPen(theme.color(theme.ON_SURFACE))
        text_rect = QRectF(18.0, 8.0, max(0.0, self.width() - 56.0), box.height() - 1.0)
        elided = QtGui.QFontMetrics(val_font).elidedText(val_text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        cx = self.width() - 22.0
        cy = box.center().y()
        chev_pen = QtGui.QPen(theme.color(theme.ON_SURFACE_VARIANT), 1.8)
        chev_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(chev_pen)
        if self._open:
            p.drawLine(QPointF(cx - 5, cy + 2), QPointF(cx, cy - 3))
            p.drawLine(QPointF(cx, cy - 3), QPointF(cx + 5, cy + 2))
        else:
            p.drawLine(QPointF(cx - 5, cy - 2), QPointF(cx, cy + 3))
            p.drawLine(QPointF(cx, cy + 3), QPointF(cx + 5, cy - 2))
        p.end()


class PathSelector(QtWidgets.QWidget):
    path_changed = Signal(Path)

    def __init__(self, initial_path: Path, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = initial_path
        self.setFixedHeight(64)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(18, 13, 12, 6)
        layout.setSpacing(8)

        self._text_lbl = QtWidgets.QLabel(self)
        self._text_lbl.setFont(theme.make_font(15, QtGui.QFont.Weight.Normal))
        self._text_lbl.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self._text_lbl, 1)

        self._btn = TextButton("Change", self)
        self._btn.setFixedHeight(36)
        self._btn.clicked.connect(self._choose_dir)
        layout.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._refresh_elide()

    @property
    def path(self) -> Path:
        return self._path

    def set_path(self, path: Path) -> None:
        if path != self._path:
            self._path = path
            self._refresh_elide()
            self.path_changed.emit(path)
        else:
            self._refresh_elide()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_elide()

    def _refresh_elide(self) -> None:
        raw = str(self._path)
        self._text_lbl.setToolTip(raw)
        avail = max(80, self.width() - self._btn.sizeHint().width() - 42)
        fm = QtGui.QFontMetrics(self._text_lbl.font())
        self._text_lbl.setText(fm.elidedText(raw, Qt.TextElideMode.ElideMiddle, avail))

    def _choose_dir(self) -> None:
        picked = QtWidgets.QFileDialog.getExistingDirectory(
            self.window(),
            "Choose download folder",
            str(self._path),
        )
        if picked:
            self.set_path(Path(picked))

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        box = QRectF(1.0, 7.0, self.width() - 2.0, self.height() - 8.0)
        p.setPen(QtGui.QPen(theme.color(theme.OUTLINE), 1.0))
        p.setBrush(theme.color(theme.SURFACE))
        p.drawRoundedRect(box, 10.0, 10.0)

        lbl = "Save to"
        lbl_font = theme.make_font(12, QtGui.QFont.Weight.Normal)
        lbl_w = QtGui.QFontMetrics(lbl_font).horizontalAdvance(lbl) + 8
        p.fillRect(QRectF(14.0, 0.0, float(lbl_w), 15.0), theme.color(theme.SURFACE))
        p.setFont(lbl_font)
        p.setPen(theme.color(theme.ON_SURFACE_VARIANT))
        p.drawText(QRectF(18.0, 0.0, float(lbl_w), 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
        p.end()


class Banner(QtWidgets.QFrame):
    def __init__(self, kind: str = "error", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        self._icon = BadgeIcon("warning", box_size=30, parent=self)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        self._label = QtWidgets.QLabel(self)
        self._label.setWordWrap(True)
        self._label.setFont(theme.make_font(14, QtGui.QFont.Weight.Normal))
        layout.addWidget(self._label, 1)

        self.set_kind(kind)
        self.hide()

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        if kind == "error":
            bg, fg, glyph = theme.ERROR_CONTAINER, theme.ON_ERROR_CONTAINER, "warning"
            icon_fg = theme.ERROR
        elif kind == "warning":
            bg, fg, glyph = theme.WARNING_CONTAINER, theme.ON_WARNING_CONTAINER, "warning"
            icon_fg = theme.WARNING
        else:
            bg, fg, glyph = theme.SURFACE_CONTAINER, theme.ON_SURFACE, "flex"
            icon_fg = theme.PRIMARY
        self._bg = bg
        self._icon.set_spec(glyph, bg, icon_fg)
        self._label.setStyleSheet(f"color: {fg}; background: transparent;")
        self.update()

    def show_message(self, text: str, kind: str | None = None) -> None:
        if kind is not None:
            self.set_kind(kind)
        if not text:
            self.hide()
            return
        self._label.setText(text)
        self.show()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color(self._bg))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 12.0, 12.0)
        p.end()


class DriveCard(QtWidgets.QWidget):
    clicked = Signal()

    def __init__(
        self,
        entry: DriveEntry,
        *,
        selected: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self._selected = selected
        self._hovered = False
        has_reason = entry.disabled_reason is not None
        self.setFixedHeight(96 if has_reason else 78)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        if entry.selectable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        else:
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        drv = entry.drive
        tip_lines = [
            f"{drv.name}",
            drv.summary_line,
        ]
        if drv.serial:
            tip_lines.append(f"Serial: {drv.serial}")
        if entry.disabled_reason:
            tip_lines.append(entry.disabled_reason)
        self.setToolTip("\n".join(tip_lines))
        self.setAccessibleName(f"{drv.name}, {drv.summary_line}")

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtGui.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.entry.selectable:
            self.clicked.emit()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.entry.selectable:
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)

        selectable = self.entry.selectable
        if not selectable:
            bg = theme.color(theme.SURFACE_LOW)
            border = theme.color(theme.DIVIDER)
            bw = 1.0
        elif self._selected:
            bg = theme.color("#EDF3FE")
            border = theme.color(theme.PRIMARY)
            bw = 1.8
        else:
            bg = theme.color(theme.SURFACE_LOW if self._hovered else theme.SURFACE)
            border = theme.color(theme.OUTLINE if self._hovered else theme.OUTLINE_VARIANT)
            bw = 1.0

        p.setPen(QtGui.QPen(border, bw))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 14.0, 14.0)

        if selectable and self.hasFocus() and not self._selected:
            p.setPen(QtGui.QPen(theme.color(theme.PRIMARY), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 13.0, 13.0)

        cy = self.height() / 2.0
        radio_c = QPointF(26.0, cy)
        if self._selected and selectable:
            p.setPen(QtGui.QPen(theme.color(theme.PRIMARY), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(radio_c, 10.0, 10.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.PRIMARY))
            p.drawEllipse(radio_c, 5.5, 5.5)
        else:
            p.setPen(QtGui.QPen(theme.color(theme.OUTLINE if selectable else theme.OUTLINE_VARIANT), 1.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(radio_c, 10.0, 10.0)

        drv = self.entry.drive
        glyph = "hdd" if drv.is_usb_hard_drive else "usb"
        glyph_col = theme.PRIMARY if (self._selected and selectable) else (theme.ON_SURFACE_VARIANT if selectable else theme.OUTLINE_VARIANT)
        theme.draw_glyph(p, glyph, QRectF(48.0, cy - 12.0, 24.0, 24.0), glyph_col, stroke_width=1.9)

        text_x = 84.0
        avail_w = max(60.0, self.width() - text_x - 18.0)

        title_font = theme.make_font(15, QtGui.QFont.Weight.Medium)
        sub_font = theme.make_font(13, QtGui.QFont.Weight.Normal)
        title_col = theme.ON_SURFACE if selectable else theme.ON_SURFACE_VARIANT
        sub_col = theme.ON_SURFACE_VARIANT if selectable else theme.OUTLINE

        has_reason = self.entry.disabled_reason is not None
        top_y = 14.0 if has_reason else 17.0

        badge_w = 0.0
        if drv.is_usb_hard_drive:
            tag_font = theme.make_font(12, QtGui.QFont.Weight.Medium)
            tag_txt = "USB HDD"
            tag_fm = QtGui.QFontMetrics(tag_font)
            tw = float(tag_fm.horizontalAdvance(tag_txt) + 16)
            badge_w = tw + 8.0
            tag_rect = QRectF(self.width() - 18.0 - tw, top_y + 1.0, tw, 21.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.SURFACE_CONTAINER_HIGH))
            p.drawRoundedRect(tag_rect, 10.5, 10.5)
            p.setFont(tag_font)
            p.setPen(theme.color(theme.ON_SURFACE_VARIANT))
            p.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag_txt)

        p.setFont(title_font)
        p.setPen(theme.color(title_col))
        name_elided = QtGui.QFontMetrics(title_font).elidedText(
            drv.name, Qt.TextElideMode.ElideRight, int(max(40.0, avail_w - badge_w))
        )
        p.drawText(QRectF(text_x, top_y, avail_w - badge_w, 22.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name_elided)

        p.setFont(sub_font)
        p.setPen(theme.color(sub_col))
        sub_elided = QtGui.QFontMetrics(sub_font).elidedText(
            drv.summary_line, Qt.TextElideMode.ElideRight, int(avail_w)
        )
        p.drawText(QRectF(text_x, top_y + 23.0, avail_w, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub_elided)

        if self.entry.disabled_reason:
            reason_font = theme.make_font(13, QtGui.QFont.Weight.Medium)
            p.setFont(reason_font)
            p.setPen(theme.color(theme.WARNING))
            r_elided = QtGui.QFontMetrics(reason_font).elidedText(
                self.entry.disabled_reason, Qt.TextElideMode.ElideRight, int(avail_w)
            )
            p.drawText(QRectF(text_x, top_y + 45.0, avail_w, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, r_elided)

        p.end()
