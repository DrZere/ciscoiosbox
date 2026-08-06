"""Live monitoring: CPU, memory and per-interface throughput."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from ..core.models import InterfaceRow, ResourceSample, TrafficSample
from ..services.monitor_service import MonitorService
from .graphs import StatRow, TimeSeriesGraph, format_bits, format_bytes
from .theme import Palette

log = logging.getLogger(__name__)


class MonitorView(QWidget):
    """Resource and traffic graphs for the connected device."""

    def __init__(self, service: MonitorService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._interfaces: list[InterfaceRow] = []
        self._build_ui()
        self._wire_service()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        layout.addLayout(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_resource_panel())
        splitter.addWidget(self._build_traffic_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Monitoring is stopped.")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.start_button = QPushButton("Start Monitoring")
        self.start_button.setProperty("accent", True)
        self.start_button.setCheckable(True)
        self.start_button.toggled.connect(self._on_toggle_monitoring)
        toolbar.addWidget(self.start_button)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("Interval"))
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(1.0, 60.0)
        self.interval_spin.setSingleStep(1.0)
        self.interval_spin.setValue(5.0)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setMaximumWidth(90)
        self.interval_spin.setToolTip(
            "How often to poll. Shorter intervals give finer detail but put more "
            "load on the device — especially over CLI.")
        self.interval_spin.valueChanged.connect(self.service.set_interval)
        toolbar.addWidget(self.interval_spin)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("Interface"))
        self.interface_combo = QComboBox()
        self.interface_combo.setMinimumWidth(200)
        self.interface_combo.currentIndexChanged.connect(self._on_interface_changed)
        toolbar.addWidget(self.interface_combo)

        toolbar.addStretch(1)

        self.source_label = QLabel()
        self.source_label.setProperty("muted", True)
        toolbar.addWidget(self.source_label)

        return toolbar

    def _build_resource_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.resource_stats = StatRow()
        self.resource_stats.add_tile("cpu", "CPU (5 sec)", Palette.SUCCESS)
        self.resource_stats.add_tile("cpu_1m", "CPU (1 min)", Palette.ACCENT)
        self.resource_stats.add_tile("mem", "Memory used", Palette.SUCCESS)
        self.resource_stats.add_tile("mem_free", "Memory free", Palette.TEXT_MUTED)
        self.resource_stats.add_stretch()
        layout.addWidget(self.resource_stats)

        graphs = QHBoxLayout()
        graphs.setSpacing(8)

        self.cpu_graph = TimeSeriesGraph(
            "CPU Utilisation", "%", capacity=240, y_range=(0, 100))
        self.cpu_graph.add_series("cpu_5sec", "5 sec", Palette.SERIES[0], fill=True)
        self.cpu_graph.add_series("cpu_1min", "1 min", Palette.SERIES[2])
        self.cpu_graph.add_series("cpu_5min", "5 min", Palette.SERIES[3], width=1.2)
        graphs.addWidget(self.cpu_graph, 1)

        self.memory_graph = TimeSeriesGraph(
            "Memory Utilisation", "%", capacity=240, y_range=(0, 100))
        self.memory_graph.add_series("used", "Used", Palette.SERIES[1], fill=True)
        graphs.addWidget(self.memory_graph, 1)

        layout.addLayout(graphs, 1)
        return panel

    def _build_traffic_panel(self) -> QWidget:
        box = QGroupBox("Interface Traffic")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        self.traffic_stats = StatRow()
        self.traffic_stats.add_tile("rx", "Inbound", Palette.SERIES[1])
        self.traffic_stats.add_tile("tx", "Outbound", Palette.SERIES[0])
        self.traffic_stats.add_tile("util", "Utilisation", Palette.SUCCESS)
        self.traffic_stats.add_tile("total", "Counters", Palette.TEXT_MUTED)
        self.traffic_stats.add_stretch()
        layout.addWidget(self.traffic_stats)

        self.traffic_graph = TimeSeriesGraph(
            "Throughput", "bits/sec", capacity=240)
        self.traffic_graph.add_series("rx", "In", Palette.SERIES[1], fill=True)
        self.traffic_graph.add_series("tx", "Out", Palette.SERIES[0])
        layout.addWidget(self.traffic_graph, 1)

        self.traffic_hint = QLabel(
            "Select an interface above to graph its throughput.")
        self.traffic_hint.setProperty("muted", True)
        layout.addWidget(self.traffic_hint)

        return box

    def _wire_service(self) -> None:
        self.service.resource_sample.connect(self._on_resource_sample)
        self.service.traffic_sample.connect(self._on_traffic_sample)
        self.service.source_changed.connect(self._on_source_changed)
        self.service.monitor_warning.connect(self._on_warning)

    # ── population ────────────────────────────────────────────────────────────

    def set_interfaces(self, interfaces: list[InterfaceRow]) -> None:
        """Fill the interface picker from the latest interface grid."""
        self._interfaces = interfaces
        current = self.interface_combo.currentData()

        self.interface_combo.blockSignals(True)
        self.interface_combo.clear()
        self.interface_combo.addItem("— none —", "")
        for row in interfaces:
            # Only offer ports that can actually carry traffic.
            if row.name.startswith("Null"):
                continue
            marker = "●" if row.is_up else "○"
            label = f"{marker} {row.short_name}"
            if row.description:
                label += f" — {row.description[:28]}"
            self.interface_combo.addItem(label, row.name)
        self.interface_combo.blockSignals(False)

        if current:
            index = self.interface_combo.findData(current)
            self.interface_combo.setCurrentIndex(max(0, index))

    def select_interface(self, name: str) -> None:
        """Focus a specific interface — used by the grid's "Graph traffic" action."""
        index = self.interface_combo.findData(name)
        if index >= 0:
            self.interface_combo.setCurrentIndex(index)
            if not self.start_button.isChecked():
                self.start_button.setChecked(True)

    # ── monitoring lifecycle ──────────────────────────────────────────────────

    def _on_toggle_monitoring(self, running: bool) -> None:
        self.start_button.setText("Stop Monitoring" if running else "Start Monitoring")
        self.start_button.setProperty("accent", not running)
        self.start_button.setProperty("danger", running)
        # Property changes need an explicit restyle to take effect.
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)

        if running:
            self.service.set_interval(self.interval_spin.value())
            self.service.start_resources()
            interface = self.interface_combo.currentData()
            if interface:
                self.service.start_traffic(interface)
            self.status_label.setText("Monitoring…")
        else:
            self.service.stop_all()
            self.status_label.setText("Monitoring is stopped.")

    def _on_interface_changed(self) -> None:
        interface = self.interface_combo.currentData() or ""
        self.traffic_graph.clear_data()
        for key in ("rx", "tx", "util", "total"):
            self.traffic_stats.set_value(key, "—", "")

        if not interface:
            self.service.stop_traffic()
            self.traffic_hint.setText("Select an interface above to graph its throughput.")
            return

        self.traffic_hint.setText(f"Graphing {interface}.")
        if self.start_button.isChecked():
            self.service.start_traffic(interface)

    def stop(self) -> None:
        """Stop polling — called when the tab or device is closed."""
        if self.start_button.isChecked():
            self.start_button.setChecked(False)
        self.service.stop_all()

    # ── sample handling ───────────────────────────────────────────────────────

    def _on_resource_sample(self, sample: ResourceSample) -> None:
        self.cpu_graph.append(sample.timestamp, {
            "cpu_5sec": sample.cpu_5sec,
            "cpu_1min": sample.cpu_1min,
            "cpu_5min": sample.cpu_5min,
        })

        from .graphs import StatTile

        self.resource_stats.set_value(
            "cpu", f"{sample.cpu_5sec:.0f}%", "five-second average",
            StatTile.colour_for_percent(sample.cpu_5sec))
        self.resource_stats.set_value(
            "cpu_1m", f"{sample.cpu_1min:.0f}%", "one-minute average",
            StatTile.colour_for_percent(sample.cpu_1min))

        if sample.mem_total_bytes:
            percent = sample.mem_used_percent
            self.memory_graph.append(sample.timestamp, {"used": percent})
            self.resource_stats.set_value(
                "mem", f"{percent:.0f}%", format_bytes(sample.mem_used_bytes),
                StatTile.colour_for_percent(percent))
            self.resource_stats.set_value(
                "mem_free", format_bytes(sample.mem_free_bytes),
                f"of {format_bytes(sample.mem_total_bytes)} total")
        else:
            # The device did not report memory in a form we recognise. Say so
            # rather than drawing a misleading flat zero line.
            self.resource_stats.set_value("mem", "n/a", "not reported by device")

        self.status_label.setText(
            f"Monitoring via {self.service.source_label} — "
            f"{self.cpu_graph.sample_count} samples.")

    def _on_traffic_sample(self, sample: TrafficSample) -> None:
        self.traffic_graph.append(sample.timestamp, {
            "rx": sample.rx_bps, "tx": sample.tx_bps,
        })

        from .graphs import StatTile

        self.traffic_stats.set_value(
            "rx", format_bits(sample.rx_bps), f"{sample.rx_pps:.0f} pps"
            if sample.rx_pps else "")
        self.traffic_stats.set_value(
            "tx", format_bits(sample.tx_bps), f"{sample.tx_pps:.0f} pps"
            if sample.tx_pps else "")

        rx_percent, tx_percent = sample.utilisation()
        if sample.bandwidth_bps > 0:
            peak = max(rx_percent, tx_percent)
            self.traffic_stats.set_value(
                "util", f"{peak:.1f}%",
                f"of {format_bits(sample.bandwidth_bps)}",
                StatTile.colour_for_percent(peak))
        else:
            self.traffic_stats.set_value("util", "—", "speed unknown")

        if sample.rx_octets is not None:
            self.traffic_stats.set_value(
                "total", format_bytes(sample.rx_octets),
                f"out {format_bytes(sample.tx_octets or 0)}")

    def _on_source_changed(self, source: str) -> None:
        self.source_label.setText(f"Source: {source}")
        tooltip = ("SNMP counter deltas — accurate and light on the device."
                   if source.startswith("SNMP")
                   else "CLI polling — uses the device's 5-minute averaged rates, "
                        "which lag short bursts.")
        self.source_label.setToolTip(tooltip)

    def _on_warning(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {Palette.WARNING};")
        # If the service gave up, reflect that in the button state.
        if not self.service.is_running and self.start_button.isChecked():
            self.start_button.setChecked(False)
