"""One connected device: its connection, services and view stack.

This is the composition root for a session. It creates the
:class:`ConnectionController`, the four services, and the views that consume
them, then wires the cross-view interactions (e.g. "graph this interface"
jumping from the grid to the monitoring tab).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.connection import ConnectionController
from ..core.exceptions import CiscoIOSBoxError
from ..core.models import ConnectionState, DeviceInfo, DeviceProfile, InterfaceRow
from ..services.interface_service import InterfaceService
from ..services.monitor_service import MonitorService
from ..services.system_service import SystemService
from ..services.vlan_service import VlanService
from .interfaces_view import InterfacesView
from .monitor_view import MonitorView
from .system_view import SystemView
from .terminal import TerminalWidget
from .theme import Palette
from .vlans_view import VlansView

log = logging.getLogger(__name__)


class DeviceTab(QWidget):
    """The workspace for a single device session."""

    #: (tab, message, level) — routed to the main window's toast manager.
    notify = Signal(object, str, str)
    #: The tab's display title changed (hostname discovered, state changed).
    title_changed = Signal(object, str)
    #: The session ended and the tab can be closed.
    closed = Signal(object)

    def __init__(self, profile: DeviceProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.device_info = DeviceInfo()
        self._interfaces: list[InterfaceRow] = []
        self._initial_load_done = False

        self.controller = ConnectionController(profile, self)

        self.interface_service = InterfaceService(self.controller, self)
        self.vlan_service = VlanService(self.controller, self)
        self.system_service = SystemService(self.controller, self)
        self.monitor_service = MonitorService(self.controller, self)

        self._build_ui()
        self._wire_controller()
        self._wire_services()
        self._wire_cross_view()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.terminal = TerminalWidget()
        self.tabs.addTab(self._wrap(self.terminal), "Terminal")

        self.interfaces_view = InterfacesView(self.interface_service)
        self.tabs.addTab(self.interfaces_view, "Interfaces")

        self.vlans_view = VlansView(self.vlan_service)
        self.tabs.addTab(self.vlans_view, "VLANs")

        self.system_view = SystemView(self.system_service)
        self.tabs.addTab(self.system_view, "System")

        self.monitor_view = MonitorView(self.monitor_service)
        self.tabs.addTab(self.monitor_view, "Monitoring")

        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

    @staticmethod
    def _wrap(widget: QWidget) -> QWidget:
        """Give a bare widget the same margins as the form-based views."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(widget)
        return container

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setStyleSheet(
            f"background-color: {Palette.BG_DEEPEST}; "
            f"border-bottom: 1px solid {Palette.BORDER};")

        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(10)

        self.state_dot = QLabel("●")
        self.state_dot.setStyleSheet(f"color: {Palette.WARNING}; font-size: 14px;")
        layout.addWidget(self.state_dot)

        self.header_label = QLabel(f"Connecting to {self.profile.display_target}…")
        font = self.header_label.font()
        font.setBold(True)
        self.header_label.setFont(font)
        layout.addWidget(self.header_label)

        self.header_detail = QLabel()
        self.header_detail.setProperty("muted", True)
        layout.addWidget(self.header_detail)

        layout.addStretch(1)

        self.transport_label = QLabel(
            f"{self.profile.connection_type.label} · {self.profile.display_target}")
        self.transport_label.setProperty("muted", True)
        layout.addWidget(self.transport_label)

        return header

    # ── wiring ────────────────────────────────────────────────────────────────

    def _wire_controller(self) -> None:
        self.controller.connected.connect(self._on_connected)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.data_received.connect(self.terminal.feed)
        self.controller.session_lost.connect(self._on_session_lost)
        self.controller.notice.connect(
            lambda message: self.notify.emit(self, message, "warning"))
        self.controller.task_failed.connect(self._on_task_failed)

        self.terminal.data_entered.connect(self.controller.send_keys)
        self.terminal.resized.connect(self._on_terminal_resized)

    def _wire_services(self) -> None:
        for service in (self.interface_service, self.vlan_service,
                        self.system_service, self.monitor_service):
            service.error.connect(self._on_service_error)

        for service in (self.interface_service, self.vlan_service,
                        self.system_service):
            if hasattr(service, "action_completed"):
                service.action_completed.connect(
                    lambda message: self.notify.emit(self, message, "success"))

        self.interface_service.interfaces_loaded.connect(self._on_interfaces_loaded)
        self.system_service.facts_loaded.connect(self._on_facts_loaded)
        self.system_view.hostname_changed.connect(self._on_hostname_changed)

    def _wire_cross_view(self) -> None:
        # Grid → monitoring: double-click or "Graph traffic".
        self.interfaces_view.monitor_requested.connect(self._show_interface_graph)
        # Grid → VLANs: "Assign VLAN to N ports".
        self.interfaces_view.assign_vlan_requested.connect(self._show_vlan_assignment)

    # ── connection lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Open the connection. Safe to call once."""
        self.terminal.write_notice(
            f"Connecting to {self.profile.display_target} "
            f"via {self.profile.connection_type.label}…\n")
        self.controller.start()

    def _on_state_changed(self, state: ConnectionState) -> None:
        colours = {
            ConnectionState.CONNECTED: Palette.SUCCESS,
            ConnectionState.CONNECTING: Palette.WARNING,
            ConnectionState.RECONNECTING: Palette.WARNING,
            ConnectionState.FAILED: Palette.DANGER,
            ConnectionState.DISCONNECTED: Palette.TEXT_FAINT,
        }
        self.state_dot.setStyleSheet(
            f"color: {colours.get(state, Palette.TEXT_FAINT)}; font-size: 14px;")
        self.terminal.set_connected(state is ConnectionState.CONNECTED)

        if state is ConnectionState.DISCONNECTED:
            self.header_label.setText("Disconnected")
            self.monitor_view.stop()

    def _on_connected(self, info: DeviceInfo) -> None:
        self.device_info = info
        hostname = info.hostname or self.profile.display_target
        self.header_label.setText(hostname)

        detail = " · ".join(filter(None, [info.model, info.version]))
        self.header_detail.setText(detail)
        self.title_changed.emit(self, hostname)

        self.terminal.write_notice(
            f"Connected to {hostname}"
            + (f" ({info.model})" if info.model else "") + "\n\n",
            Palette.SUCCESS)

        self.system_view.set_facts(info)
        self.monitor_service.configure()
        self.notify.emit(self, f"Connected to {hostname}.", "success")

        # Load the data the other tabs need, once, in the background. The user
        # lands on the terminal, so this is invisible unless they switch tabs.
        if not self._initial_load_done:
            self._initial_load_done = True
            self.interface_service.refresh()
            self.vlan_service.refresh()
            self.system_service.check_unsaved_changes()

    def _on_session_lost(self, reason: str) -> None:
        self.terminal.write_notice(f"\n[ Connection lost: {reason} ]\n", Palette.DANGER)
        self.notify.emit(self, f"{self.profile.name}: {reason}", "error")
        self.monitor_view.stop()
        self.title_changed.emit(self, f"{self.header_label.text()} (offline)")

    def _on_task_failed(self, task_id: str, kind: str, exc: object) -> None:
        # Connection failures are the only ones not owned by a service, so they
        # would otherwise go unreported.
        if kind != "connect":
            return
        message = getattr(exc, "user_message", str(exc))
        detail = getattr(exc, "detail", "")
        self.terminal.write_notice(f"\n[ Connection failed: {message} ]\n", Palette.DANGER)
        self.header_label.setText("Connection failed")
        self.header_detail.setText(message)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Connection Failed")
        box.setText(f"Could not connect to {self.profile.name}.")
        box.setInformativeText(message)
        if detail:
            box.setDetailedText(str(detail))
        box.exec()

    def _on_service_error(self, message: str, kind: str) -> None:
        self.notify.emit(self, message, "error")

    # ── data flow between views ───────────────────────────────────────────────

    def _on_interfaces_loaded(self, rows: list[InterfaceRow]) -> None:
        """Fan the interface list out to every view that needs it."""
        self._interfaces = rows
        self.vlans_view.set_interfaces(rows)
        self.monitor_view.set_interfaces(rows)
        self.system_view.set_interfaces(rows)

        # Tell the system view which interface we are connected through, so it
        # can warn before the user changes that address out from under us.
        connected_via = self._detect_connected_interface(rows)
        if connected_via:
            self.system_view.set_connected_interface(connected_via)

    def _detect_connected_interface(self, rows: list[InterfaceRow]) -> str:
        """Find the interface whose IP matches the host we connected to."""
        if not self.profile.host:
            return ""
        for row in rows:
            if row.ip_address and row.ip_address == self.profile.host:
                return row.name
        return ""

    def _on_facts_loaded(self, info: DeviceInfo) -> None:
        self.device_info = info
        if info.hostname:
            self.header_label.setText(info.hostname)
            self.title_changed.emit(self, info.hostname)

    def _on_hostname_changed(self, hostname: str) -> None:
        self.header_label.setText(hostname)
        self.title_changed.emit(self, hostname)

    def _show_interface_graph(self, interface: str) -> None:
        self.tabs.setCurrentWidget(self.monitor_view)
        self.monitor_view.select_interface(interface)

    def _show_vlan_assignment(self, interfaces: list[str]) -> None:
        self.tabs.setCurrentWidget(self.vlans_view)
        self.vlans_view.preselect_ports(interfaces)

    def _on_tab_changed(self, index: int) -> None:
        """Only stream the channel while the terminal is actually visible."""
        is_terminal = self.tabs.widget(index) is not None and \
            self.tabs.tabText(index) == "Terminal"
        self.controller.set_streaming(is_terminal)
        if is_terminal:
            self.terminal.setFocus()

    def _on_terminal_resized(self, columns: int, rows: int) -> None:
        if self.controller.is_connected:
            self.controller.submit(
                lambda transport: transport.resize_pty(columns, rows),
                kind="terminal_resize", priority=1)

    # ── teardown ──────────────────────────────────────────────────────────────

    def refresh_all(self) -> None:
        """Reload every data view. Bound to F5 in the main window."""
        if not self.controller.is_connected:
            return
        self.interface_service.refresh()
        self.vlan_service.refresh()
        self.system_service.refresh_facts()

    def disconnect_device(self) -> None:
        """Close the session but keep the tab open so output stays readable."""
        self.monitor_view.stop()
        self.controller.shutdown()

    def close_tab(self) -> None:
        """Tear everything down ahead of the tab being removed."""
        try:
            self.monitor_view.stop()
        except Exception:  # noqa: BLE001 - teardown must not raise
            log.debug("Error stopping monitor", exc_info=True)
        self.controller.shutdown()
        self.closed.emit(self)

    @property
    def is_connected(self) -> bool:
        return self.controller.is_connected

    @property
    def title(self) -> str:
        return self.header_label.text() or self.profile.name
