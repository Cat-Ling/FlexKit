from __future__ import annotations

from flexkit import assets_dir
from flexkit.ui.qt import QPointF, QRectF, QSize, Qt, QtGui, QtWidgets

PRIMARY = "#0B57D0"
ON_PRIMARY = "#FFFFFF"
PRIMARY_CONTAINER = "#D3E3FD"
ON_PRIMARY_CONTAINER = "#041E49"

SURFACE = "#FFFFFF"
SURFACE_LOW = "#F8FAFD"
SURFACE_CONTAINER = "#F0F4F9"
SURFACE_CONTAINER_HIGH = "#E9EEF6"
ON_SURFACE = "#1F1F1F"
ON_SURFACE_VARIANT = "#444746"

OUTLINE = "#747775"
OUTLINE_VARIANT = "#C4C7C5"
DIVIDER = "#E1E3E1"

ERROR = "#B3261E"
ON_ERROR = "#FFFFFF"
ERROR_CONTAINER = "#F9DEDC"
ON_ERROR_CONTAINER = "#410E0B"

WARNING = "#B06000"
WARNING_CONTAINER = "#FEF7E0"
ON_WARNING_CONTAINER = "#422000"

SUCCESS = "#146C2E"
SUCCESS_CONTAINER = "#E6F4EA"
ON_SUCCESS_CONTAINER = "#072711"

SCRIM = "#52000000"

_FONT_FAMILY: str | None = None


def color(hex_str: str, alpha: float | None = None) -> QtGui.QColor:
    c = QtGui.QColor(hex_str)
    if alpha is not None:
        c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def font_family() -> str:
    return _FONT_FAMILY or "Sans Serif"


def make_font(
    pixel_size: int,
    weight: QtGui.QFont.Weight = QtGui.QFont.Weight.Normal,
    *,
    tabular: bool = False,
) -> QtGui.QFont:
    f = QtGui.QFont(font_family())
    f.setPixelSize(pixel_size)
    f.setWeight(weight)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferNoHinting)
    if tabular:
        f.setStyleHint(QtGui.QFont.StyleHint.SansSerif)
    return f


def apply_theme(app: QtWidgets.QApplication) -> None:
    global _FONT_FAMILY
    app.setStyle("Fusion")

    hints = app.styleHints()
    set_scheme = getattr(hints, "setColorScheme", None)
    color_scheme_enum = getattr(Qt, "ColorScheme", None)
    if set_scheme is not None and color_scheme_enum is not None:
        set_scheme(color_scheme_enum.Light)

    font_path = assets_dir() / "fonts" / "GoogleSansFlex.ttf"
    if font_path.is_file():
        fid = QtGui.QFontDatabase.addApplicationFont(str(font_path))
        if fid != -1:
            fams = QtGui.QFontDatabase.applicationFontFamilies(fid)
            if fams:
                _FONT_FAMILY = fams[0]

    app.setFont(make_font(14, QtGui.QFont.Weight.Normal))

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, color(SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, color(ON_SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.Base, color(SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, color(SURFACE_CONTAINER))
    pal.setColor(QtGui.QPalette.ColorRole.Text, color(ON_SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.Button, color(SURFACE_CONTAINER))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, color(ON_SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, color(PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, color(ON_PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, color(ON_SURFACE_VARIANT))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, color(ON_SURFACE))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, color(SURFACE))
    app.setPalette(pal)

    app.setStyleSheet(
        f"""
        QWidget {{
            color: {ON_SURFACE};
        }}
        QScrollArea {{
            border: none;
            background: transparent;
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 8px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {OUTLINE_VARIANT};
            min-height: 28px;
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {OUTLINE};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
            height: 0px;
        }}
        QToolTip {{
            background-color: #303030;
            color: #FFFFFF;
            border: none;
            border-radius: 4px;
            padding: 4px 8px;
        }}
        """
    )


def render_app_icon(size: int = 64) -> QtGui.QIcon:
    icon = QtGui.QIcon()
    for px in (16, 24, 32, 48, 64, 128, 256):
        pm = QtGui.QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        s = px / 128.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(PRIMARY))
        p.drawRoundedRect(QRectF(8 * s, 8 * s, 112 * s, 112 * s), 28 * s, 28 * s)

        p.setBrush(color(PRIMARY_CONTAINER))
        p.drawRoundedRect(QRectF(42 * s, 24 * s, 44 * s, 24 * s), 5 * s, 5 * s)
        p.setBrush(color(PRIMARY))
        p.drawRoundedRect(QRectF(50 * s, 31 * s, 8 * s, 10 * s), 2 * s, 2 * s)
        p.drawRoundedRect(QRectF(70 * s, 31 * s, 8 * s, 10 * s), 2 * s, 2 * s)

        p.setBrush(color(SURFACE))
        p.drawRoundedRect(QRectF(34 * s, 46 * s, 60 * s, 58 * s), 14 * s, 14 * s)

        pen = QtGui.QPen(color(PRIMARY), max(1.5, 6.5 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(64 * s, 58 * s), QPointF(64 * s, 80 * s))
        p.drawLine(QPointF(54 * s, 70 * s), QPointF(64 * s, 80 * s))
        p.drawLine(QPointF(74 * s, 70 * s), QPointF(64 * s, 80 * s))
        p.drawLine(QPointF(49 * s, 90 * s), QPointF(79 * s, 90 * s))
        p.end()
        icon.addPixmap(pm)
    _ = size
    return icon


def draw_glyph(
    painter: QtGui.QPainter,
    kind: str,
    rect: QRectF,
    stroke_hex: str,
    *,
    stroke_width: float = 1.9,
) -> None:
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(color(stroke_hex), stroke_width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    cx, cy = x + w / 2.0, y + h / 2.0

    if kind == "flex":
        painter.drawRoundedRect(QRectF(x + w * 0.28, y + h * 0.12, w * 0.44, h * 0.22), 3.0, 3.0)
        painter.drawRoundedRect(QRectF(x + w * 0.20, y + h * 0.34, w * 0.60, h * 0.54), 5.0, 5.0)
        painter.drawLine(QPointF(cx, y + h * 0.46), QPointF(cx, y + h * 0.70))
        painter.drawLine(QPointF(cx - w * 0.12, y + h * 0.60), QPointF(cx, y + h * 0.71))
        painter.drawLine(QPointF(cx + w * 0.12, y + h * 0.60), QPointF(cx, y + h * 0.71))
    elif kind == "download":
        painter.drawLine(QPointF(cx, y + h * 0.18), QPointF(cx, y + h * 0.66))
        painter.drawLine(QPointF(cx - w * 0.20, y + h * 0.48), QPointF(cx, y + h * 0.68))
        painter.drawLine(QPointF(cx + w * 0.20, y + h * 0.48), QPointF(cx, y + h * 0.68))
        painter.drawLine(QPointF(x + w * 0.22, y + h * 0.82), QPointF(x + w * 0.78, y + h * 0.82))
    elif kind == "usb":
        painter.drawRoundedRect(QRectF(x + w * 0.30, y + h * 0.36, w * 0.40, h * 0.48), 4.0, 4.0)
        painter.drawRect(QRectF(x + w * 0.36, y + h * 0.16, w * 0.28, h * 0.20))
        painter.drawPoint(QPointF(x + w * 0.44, y + h * 0.26))
        painter.drawPoint(QPointF(x + w * 0.56, y + h * 0.26))
    elif kind == "hdd":
        painter.drawRoundedRect(QRectF(x + w * 0.18, y + h * 0.22, w * 0.64, h * 0.56), 4.0, 4.0)
        painter.drawLine(QPointF(x + w * 0.18, y + h * 0.58), QPointF(x + w * 0.82, y + h * 0.58))
        painter.drawEllipse(QPointF(x + w * 0.68, y + h * 0.68), w * 0.04, w * 0.04)
    elif kind == "check":
        path = QtGui.QPainterPath()
        path.moveTo(x + w * 0.24, cy + h * 0.02)
        path.lineTo(x + w * 0.43, y + h * 0.70)
        path.lineTo(x + w * 0.77, y + h * 0.32)
        painter.drawPath(path)
    elif kind == "verify":
        path = QtGui.QPainterPath()
        path.moveTo(cx, y + h * 0.14)
        path.lineTo(x + w * 0.80, y + h * 0.26)
        path.lineTo(x + w * 0.80, y + h * 0.52)
        path.quadTo(x + w * 0.80, y + h * 0.78, cx, y + h * 0.88)
        path.quadTo(x + w * 0.20, y + h * 0.78, x + w * 0.20, y + h * 0.52)
        path.lineTo(x + w * 0.20, y + h * 0.26)
        path.closeSubpath()
        painter.drawPath(path)
        chk = QtGui.QPainterPath()
        chk.moveTo(x + w * 0.36, cy + h * 0.02)
        chk.lineTo(x + w * 0.47, y + h * 0.62)
        chk.lineTo(x + w * 0.66, y + h * 0.40)
        painter.drawPath(chk)
    elif kind == "warning":
        tri = QtGui.QPainterPath()
        tri.moveTo(cx, y + h * 0.16)
        tri.lineTo(x + w * 0.86, y + h * 0.82)
        tri.lineTo(x + w * 0.14, y + h * 0.82)
        tri.closeSubpath()
        painter.drawPath(tri)
        painter.drawLine(QPointF(cx, y + h * 0.42), QPointF(cx, y + h * 0.60))
        painter.drawPoint(QPointF(cx, y + h * 0.71))
    elif kind == "lock":
        painter.drawRoundedRect(QRectF(x + w * 0.24, y + h * 0.44, w * 0.52, h * 0.40), 4.0, 4.0)
        arc = QtGui.QPainterPath()
        arc.moveTo(x + w * 0.34, y + h * 0.44)
        arc.lineTo(x + w * 0.34, y + h * 0.32)
        arc.arcTo(QRectF(x + w * 0.34, y + h * 0.16, w * 0.32, h * 0.32), 180, -180)
        arc.lineTo(x + w * 0.66, y + h * 0.44)
        painter.drawPath(arc)
    elif kind == "folder":
        path = QtGui.QPainterPath()
        path.moveTo(x + w * 0.14, y + h * 0.28)
        path.lineTo(x + w * 0.40, y + h * 0.28)
        path.lineTo(x + w * 0.48, y + h * 0.37)
        path.lineTo(x + w * 0.86, y + h * 0.37)
        path.lineTo(x + w * 0.86, y + h * 0.78)
        path.lineTo(x + w * 0.14, y + h * 0.78)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "refresh":
        painter.drawArc(QRectF(x + w * 0.20, y + h * 0.20, w * 0.60, h * 0.60), 45 * 16, 285 * 16)
        painter.drawLine(QPointF(x + w * 0.62, y + h * 0.18), QPointF(x + w * 0.78, y + h * 0.22))
        painter.drawLine(QPointF(x + w * 0.74, y + h * 0.38), QPointF(x + w * 0.78, y + h * 0.22))

    painter.restore()


__all__ = [
    "DIVIDER",
    "ERROR",
    "ERROR_CONTAINER",
    "ON_ERROR",
    "ON_ERROR_CONTAINER",
    "ON_PRIMARY",
    "ON_PRIMARY_CONTAINER",
    "ON_SURFACE",
    "ON_SURFACE_VARIANT",
    "ON_SUCCESS_CONTAINER",
    "ON_WARNING_CONTAINER",
    "OUTLINE",
    "OUTLINE_VARIANT",
    "PRIMARY",
    "PRIMARY_CONTAINER",
    "QSize",
    "SCRIM",
    "SUCCESS",
    "SUCCESS_CONTAINER",
    "SURFACE",
    "SURFACE_CONTAINER",
    "SURFACE_CONTAINER_HIGH",
    "SURFACE_LOW",
    "WARNING",
    "WARNING_CONTAINER",
    "apply_theme",
    "color",
    "draw_glyph",
    "font_family",
    "make_font",
    "render_app_icon",
]
