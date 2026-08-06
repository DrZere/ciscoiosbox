"""Real-time graph widgets built on pyqtgraph.

Each graph owns a fixed-length ring buffer of samples, so memory stays flat no
matter how long a session runs. The x-axis is seconds-ago rather than wall
clock: for live monitoring, "30 seconds back" is the question being asked, and
it avoids the axis relabelling on every tick that makes timestamps flicker.
"""
from __future__ import annotations

import logging
from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .theme import Palette

log = logging.getLogger(__name__)

# Global pyqtgraph defaults, applied once at import so every plot matches the
# app's dark theme rather than pyqtgraph's white default.
pg.setConfigOptions(
    background=Palette.BG_DEEPEST,
    foreground=Palette.TEXT_MUTED,
    antialias=True,
)


def format_bits(value: float) -> str:
    """Human-readable bit rate: 1500000 → '1.50 Mbps'."""
    for unit, threshold in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if abs(value) >= threshold:
            return f"{value / threshold:.2f} {unit}"
    return f"{value:.0f} bps"


def format_bytes(value: float) -> str:
    """Human-readable byte count: 74253464 → '70.8 MB'."""
    for unit, threshold in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if abs(value) >= threshold:
            return f"{value / threshold:.1f} {unit}"
    return f"{value:.0f} B"


class TimeSeriesGraph(pg.PlotWidget):
    """A live line chart with a fixed-capacity history."""

    def __init__(self, title: str, y_label: str, *, capacity: int = 180,
                 y_range: tuple[float, float] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._capacity = capacity
        self._series: dict[str, tuple[deque, pg.PlotDataItem]] = {}
        self._times: deque[float] = deque(maxlen=capacity)

        self.setTitle(title, color=Palette.TEXT, size="10pt")
        self.setLabel("left", y_label)
        self.setLabel("bottom", "seconds ago")
        self.showGrid(x=True, y=True, alpha=0.15)
        self.setMenuEnabled(False)
        # Live data should not fight the user's mouse; panning a graph that
        # rewrites itself every few seconds is never useful.
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()

        if y_range is not None:
            self.setYRange(*y_range, padding=0.02)
            self._fixed_y = True
        else:
            self._fixed_y = False
            self.enableAutoRange(axis="y")

        self.addLegend(offset=(-10, 10), labelTextColor=Palette.TEXT_MUTED)
        self.getAxis("left").setTextPen(Palette.TEXT_MUTED)
        self.getAxis("bottom").setTextPen(Palette.TEXT_MUTED)

    def add_series(self, key: str, label: str, colour: str, *,
                   width: float = 2.0, fill: bool = False) -> None:
        """Register a line. Call once per series before feeding data."""
        pen = pg.mkPen(color=colour, width=width)
        brush = pg.mkBrush(pg.mkColor(colour).darker(260)) if fill else None
        curve = self.plot([], [], pen=pen, name=label,
                          fillLevel=0.0 if fill else None, brush=brush)
        self._series[key] = (deque(maxlen=self._capacity), curve)

    def append(self, timestamp: float, values: dict[str, float]) -> None:
        """Add one sample. Missing keys reuse their previous value."""
        self._times.append(timestamp)
        for key, (buffer, _) in self._series.items():
            if key in values:
                buffer.append(float(values[key]))
            else:
                buffer.append(buffer[-1] if buffer else 0.0)
        self._redraw()

    def _redraw(self) -> None:
        if not self._times:
            return
        now = self._times[-1]
        # Negative x = further in the past, so the newest sample sits at 0 on
        # the right and history scrolls left.
        x = np.fromiter((t - now for t in self._times), dtype=float,
                        count=len(self._times))

        for buffer, curve in self._series.values():
            if not buffer:
                continue
            y = np.fromiter(buffer, dtype=float, count=len(buffer))
            # Buffers can differ in length by one during a partial update.
            length = min(len(x), len(y))
            curve.setData(x[-length:], y[-length:])

        span = max(10.0, now - self._times[0])
        self.setXRange(-span, 0, padding=0.01)

    def clear_data(self) -> None:
        self._times.clear()
        for buffer, curve in self._series.values():
            buffer.clear()
            curve.setData([], [])

    @property
    def sample_count(self) -> int:
        return len(self._times)


class StatTile(QWidget):
    """A single headline number with a label and optional secondary line."""

    def __init__(self, label: str, colour: str = Palette.ACCENT,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colour = colour

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(1)

        self.label_widget = QLabel(label.upper())
        self.label_widget.setStyleSheet(
            f"color: {Palette.TEXT_FAINT}; font-size: 10px; "
            f"font-weight: 600; letter-spacing: 0.6px;")
        layout.addWidget(self.label_widget)

        self.value_widget = QLabel("—")
        self.value_widget.setStyleSheet(
            f"color: {colour}; font-size: 21px; font-weight: 600;")
        layout.addWidget(self.value_widget)

        self.detail_widget = QLabel("")
        self.detail_widget.setStyleSheet(
            f"color: {Palette.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self.detail_widget)

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            StatTile {{
                background-color: {Palette.BG_RAISED};
                border: 1px solid {Palette.BORDER};
                border-radius: 6px;
            }}
        """)
        self.setMinimumWidth(130)

    def set_value(self, value: str, detail: str = "",
                  colour: str | None = None) -> None:
        self.value_widget.setText(value)
        self.detail_widget.setText(detail)
        if colour is not None and colour != self._colour:
            self._colour = colour
            self.value_widget.setStyleSheet(
                f"color: {colour}; font-size: 21px; font-weight: 600;")

    @staticmethod
    def colour_for_percent(percent: float) -> str:
        """Green → amber → red as a utilisation figure climbs."""
        if percent >= 90:
            return Palette.DANGER
        if percent >= 75:
            return Palette.WARNING
        return Palette.SUCCESS


class StatRow(QWidget):
    """A horizontal row of :class:`StatTile` widgets."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self.tiles: dict[str, StatTile] = {}

    def add_tile(self, key: str, label: str,
                 colour: str = Palette.ACCENT) -> StatTile:
        tile = StatTile(label, colour, self)
        self.tiles[key] = tile
        self._layout.addWidget(tile)
        return tile

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    def set_value(self, key: str, value: str, detail: str = "",
                  colour: str | None = None) -> None:
        tile = self.tiles.get(key)
        if tile is not None:
            tile.set_value(value, detail, colour)
