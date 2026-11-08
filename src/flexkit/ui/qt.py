from __future__ import annotations

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Property, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl
    from PySide6.QtCore import Signal, Slot

    QT_API = "PySide6"
except ImportError:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]
    from PyQt6.QtCore import (  # type: ignore[no-redef]
         QObject,
        QPoint,
        QPointF,
        QRect,
        QRectF,
        QSize,
        Qt,
        QThread,
        QTimer,
        QUrl,
        pyqtProperty as Property,
        pyqtSignal as Signal,
        pyqtSlot as Slot,
    )

    QT_API = "PyQt6"

__all__ = [
    "Property",
    "QObject",
    "QPoint",
    "QPointF",
    "QRect",
    "QRectF",
    "QSize",
    "QT_API",
    "QThread",
    "QTimer",
    "QUrl",
    "Qt",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "Signal",
    "Slot",
]
