"""Transient notification banners.

Used for outcomes the user should notice but need not acknowledge — "VLAN 10
created", "connection lost". Anything requiring a decision uses a real modal
dialog instead, so a toast is never load-bearing.

Toasts are rendered as small popup windows, not as children of the host window.
A child overlay pinned to the bottom-right corner would sit on top of whatever
control happens to live there (the VLAN tab's Apply button, the System tab's
"Save to File…" button) and swallow its clicks, so part of the interface would
appear dead while a notification was up.
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QWidget,
)

from ..theme import Palette


class ToastLevel(Enum):
    INFO = ("ℹ", Palette.INFO)
    SUCCESS = ("✓", Palette.SUCCESS)
    WARNING = ("⚠", Palette.WARNING)
    ERROR = ("✕", Palette.DANGER)

    @property
    def icon(self) -> str:
        return self.value[0]

    @property
    def colour(self) -> str:
        return self.value[1]


class Toast(QWidget):
    """A single notification banner, shown as a frameless popup window.

    Being a top-level ``Qt.Tool`` window keeps it out of the host window's
    widget tree, so it never intercepts mouse events meant for the controls
    underneath — while still closing automatically with the application.
    """

    dismissed = Signal(object)

    def __init__(self, message: str, level: ToastLevel = ToastLevel.INFO,
                 timeout_ms: int = 5000, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        # A popup that steals focus would interrupt typing in the terminal.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.level = level
        self._dismissing = False
        self.setStyleSheet(f"""
            Toast {{
                background-color: {Palette.BG_OVERLAY};
                border: 1px solid {level.colour};
                border-left: 3px solid {level.colour};
                border-radius: 6px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 8, 9)
        layout.setSpacing(10)

        icon = QLabel(level.icon)
        icon.setStyleSheet(f"color: {level.colour}; font-size: 15px; font-weight: bold;")
        icon.setFixedWidth(18)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(icon)

        self.label = QLabel(message)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color: {Palette.TEXT}; background: transparent;")
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)

        close = QPushButton("✕")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: {Palette.TEXT_FAINT}; font-size: 12px; padding: 0;
            }}
            QPushButton:hover {{ color: {Palette.TEXT}; }}
        """)
        close.clicked.connect(self.dismiss)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

        self.setFixedWidth(360)
        self.setMaximumHeight(200)

        # Fade with the native window opacity. A QGraphicsOpacityEffect would
        # need a translucent-background window to composite on macOS, and that
        # combination is unreliable for top-level windows (they can come up
        # blank or black), so animate setWindowOpacity() instead.
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setWindowOpacity(0.0)

        # Errors persist until dismissed: they usually need acting on, and a
        # message that vanishes before it is read is worse than none.
        if timeout_ms > 0 and level is not ToastLevel.ERROR:
            QTimer.singleShot(timeout_ms, self.dismiss)

    def show_animated(self) -> None:
        self.show()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def dismiss(self) -> None:
        if self._dismissing:
            return
        self._dismissing = True
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self) -> None:
        # Keep _dismissing set: the toast is about to be deleted, and a stray
        # dismiss() call in the meantime must not restart the fade on a doomed
        # object.
        self.dismissed.emit(self)
        self.hide()
        self.deleteLater()


class ToastManager(QWidget):
    """Stacks toasts as popup windows at the host window's bottom-right corner.

    The manager widget itself is never drawn and never intercepts mouse events;
    it only owns the toasts and recomputes their positions when the host window
    moves or resizes.
    """

    MAX_VISIBLE = 4
    _MARGIN = 18
    _SPACING = 8
    #: Extra offset so the stack clears the status bar.
    _BOTTOM_OFFSET = 24

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._toasts: list[Toast] = []

        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        parent.installEventFilter(self)

    def show_toast(self, message: str, level: ToastLevel = ToastLevel.INFO,
                   timeout_ms: int = 5000) -> None:
        # Retire the oldest when the stack is full, so notifications can never
        # grow to cover the window they are annotating.
        while len(self._toasts) >= self.MAX_VISIBLE:
            self._toasts[0].dismiss()
            self._toasts.pop(0)

        toast = Toast(message, level, timeout_ms, self)
        toast.dismissed.connect(self._remove)
        self._toasts.append(toast)
        self._reposition()
        toast.show_animated()
        # Cheap insurance against a fresh tool window briefly appearing behind
        # the main window on the first show.
        toast.raise_()

    def info(self, message: str) -> None:
        self.show_toast(message, ToastLevel.INFO)

    def success(self, message: str) -> None:
        self.show_toast(message, ToastLevel.SUCCESS)

    def warning(self, message: str) -> None:
        self.show_toast(message, ToastLevel.WARNING, timeout_ms=8000)

    def error(self, message: str) -> None:
        self.show_toast(message, ToastLevel.ERROR, timeout_ms=0)

    def _remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._reposition()

    def _reposition(self) -> None:
        """Stack the popups above the host window's bottom-right corner."""
        host = self.parentWidget()
        if host is None or not host.isVisible():
            return
        origin = host.mapToGlobal(host.rect().topLeft())
        right = origin.x() + host.width() - self._MARGIN
        bottom = origin.y() + host.height() - self._MARGIN - self._BOTTOM_OFFSET

        # Newest toast goes lowest, nearest the corner (matches the original
        # layout where toasts were appended beneath the stack).
        for toast in reversed(self._toasts):
            if toast._dismissing:
                continue                # fade out in place; _remove restacks
            toast.adjustSize()
            toast.move(right - toast.width(), bottom - toast.height())
            bottom -= toast.height() + self._SPACING

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        from PySide6.QtCore import QEvent

        if obj is self.parentWidget() and event.type() in (
                QEvent.Type.Resize, QEvent.Type.Move):
            self._reposition()
        return False
