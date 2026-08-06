"""Basic system configuration: hostname, management IP, saving config."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ..core.models import DeviceInfo, InterfaceRow
from ..parsers import system as parse_sys
from ..services.system_service import SystemService
from .theme import Palette, monospace_font

log = logging.getLogger(__name__)


class SystemView(QWidget):
    """Device facts plus the small set of config changes most often needed."""

    #: Emitted when the hostname changes, so the tab label can follow.
    hostname_changed = Signal(str)

    def __init__(self, service: SystemService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._interfaces: list[InterfaceRow] = []
        self._connected_via = ""       # interface we are reaching the device through
        self._build_ui()
        self._wire_service()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        layout.addWidget(self._build_facts_box())
        layout.addWidget(self._build_hostname_box())
        layout.addWidget(self._build_management_box())
        layout.addWidget(self._build_config_box())
        layout.addStretch(1)

        self.status_label = QLabel()
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_facts_box(self) -> QWidget:
        box = QGroupBox("Device Information")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(18)

        self.fact_labels: dict[str, QLabel] = {}
        facts = [
            ("hostname", "Hostname"), ("model", "Model"),
            ("version", "IOS version"), ("serial_number", "Serial number"),
            ("uptime", "Uptime"), ("image", "Image"),
        ]
        for index, (key, label) in enumerate(facts):
            row, column = divmod(index, 2)
            caption = QLabel(f"{label}:")
            caption.setProperty("muted", True)
            grid.addWidget(caption, row, column * 2, Qt.AlignmentFlag.AlignRight)

            value = QLabel("—")
            value.setFont(monospace_font(11))
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            self.fact_labels[key] = value
            grid.addWidget(value, row, column * 2 + 1)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.service.refresh_facts)
        grid.addWidget(refresh, 3, 3, Qt.AlignmentFlag.AlignRight)

        return box

    def _build_hostname_box(self) -> QWidget:
        box = QGroupBox("Hostname")
        layout = QHBoxLayout(box)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("switch-01")
        self.hostname_edit.textChanged.connect(self._validate_hostname)
        self.hostname_edit.returnPressed.connect(self._apply_hostname)
        layout.addWidget(self.hostname_edit, 1)

        self.hostname_button = QPushButton("Apply")
        self.hostname_button.setProperty("accent", True)
        self.hostname_button.setEnabled(False)
        self.hostname_button.clicked.connect(self._apply_hostname)
        layout.addWidget(self.hostname_button)

        self.hostname_error = QLabel()
        self.hostname_error.setProperty("error", True)
        layout.addWidget(self.hostname_error)

        return box

    def _build_management_box(self) -> QWidget:
        box = QGroupBox("Management IP")
        layout = QVBoxLayout(box)

        form = QFormLayout()

        self.mgmt_interface_combo = QComboBox()
        self.mgmt_interface_combo.setEditable(True)
        self.mgmt_interface_combo.setToolTip(
            "The interface carrying the management address — usually an SVI "
            "such as Vlan1, or a dedicated management port.")
        form.addRow("Interface", self.mgmt_interface_combo)

        self.dhcp_check = QCheckBox("Obtain an address via DHCP")
        self.dhcp_check.toggled.connect(self._on_dhcp_toggled)
        form.addRow("", self.dhcp_check)

        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("192.168.1.10")
        self.ip_edit.textChanged.connect(self._validate_management)
        self.ip_label = QLabel("IP address")
        form.addRow(self.ip_label, self.ip_edit)

        self.mask_edit = QLineEdit()
        self.mask_edit.setPlaceholderText("255.255.255.0")
        self.mask_edit.setText("255.255.255.0")
        self.mask_edit.textChanged.connect(self._validate_management)
        self.mask_label = QLabel("Subnet mask")
        form.addRow(self.mask_label, self.mask_edit)

        self.gateway_edit = QLineEdit()
        self.gateway_edit.setPlaceholderText("192.168.1.1  (optional)")
        self.gateway_edit.textChanged.connect(self._validate_management)
        form.addRow("Default gateway", self.gateway_edit)

        layout.addLayout(form)

        self.mgmt_warning = QLabel()
        self.mgmt_warning.setWordWrap(True)
        self.mgmt_warning.hide()
        layout.addWidget(self.mgmt_warning)

        buttons = QHBoxLayout()
        reload_button = QPushButton("Read from Device")
        reload_button.clicked.connect(self._load_management)
        buttons.addWidget(reload_button)
        buttons.addStretch(1)

        self.mgmt_button = QPushButton("Apply")
        self.mgmt_button.setProperty("accent", True)
        self.mgmt_button.setEnabled(False)
        self.mgmt_button.clicked.connect(self._apply_management)
        buttons.addWidget(self.mgmt_button)
        layout.addLayout(buttons)

        return box

    def _build_config_box(self) -> QWidget:
        box = QGroupBox("Configuration")
        layout = QVBoxLayout(box)

        buttons = QHBoxLayout()

        self.save_button = QPushButton("Save Running Config")
        self.save_button.setProperty("accent", True)
        self.save_button.setToolTip("copy running-config startup-config")
        self.save_button.clicked.connect(self._save_config)
        buttons.addWidget(self.save_button)

        view_button = QPushButton("View Running Config")
        view_button.clicked.connect(self.service.load_running_config)
        buttons.addWidget(view_button)

        buttons.addStretch(1)

        self.unsaved_label = QLabel()
        buttons.addWidget(self.unsaved_label)
        layout.addLayout(buttons)

        self.config_view = QPlainTextEdit()
        self.config_view.setReadOnly(True)
        self.config_view.setFont(monospace_font(10))
        self.config_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.config_view.setPlaceholderText(
            "Click “View Running Config” to fetch the device's configuration.")
        self.config_view.setMinimumHeight(220)
        layout.addWidget(self.config_view)

        export_row = QHBoxLayout()
        export_row.addStretch(1)
        self.export_button = QPushButton("Save to File…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_config)
        export_row.addWidget(self.export_button)
        layout.addLayout(export_row)

        return box

    def _wire_service(self) -> None:
        self.service.facts_loaded.connect(self.set_facts)
        self.service.mgmt_loaded.connect(self._on_mgmt_loaded)
        self.service.running_config_loaded.connect(self._on_config_loaded)
        self.service.unsaved_changes.connect(self._on_unsaved_changes)
        self.service.hostname_changed.connect(self._on_hostname_applied)
        self.service.busy_changed.connect(self._on_busy)

    # ── population ────────────────────────────────────────────────────────────

    def set_facts(self, info: DeviceInfo) -> None:
        for key, label in self.fact_labels.items():
            value = getattr(info, key, "") or "—"
            label.setText(str(value))
        if info.hostname and not self.hostname_edit.text():
            self.hostname_edit.setText(info.hostname)

        if not info.in_enable_mode:
            self.status_label.setText(
                "Not in privileged EXEC mode — configuration changes will be "
                "rejected. Reconnect with an enable secret to make changes.")
            self.status_label.setStyleSheet(f"color: {Palette.WARNING};")
            for widget in (self.hostname_button, self.mgmt_button, self.save_button):
                widget.setEnabled(False)
                widget.setToolTip("Requires privileged EXEC mode (enable).")

    def set_interfaces(self, interfaces: list[InterfaceRow]) -> None:
        """Fill the management-interface picker."""
        self._interfaces = interfaces
        current = self.mgmt_interface_combo.currentText()

        self.mgmt_interface_combo.clear()
        # Put the likely management interfaces first — SVIs and mgmt ports.
        def rank(row: InterfaceRow) -> tuple[int, str]:
            if row.name.startswith("Vlan"):
                return (0, row.name)
            if "anagement" in row.name:
                return (1, row.name)
            if row.ip_address:
                return (2, row.name)
            return (3, row.name)

        for row in sorted(interfaces, key=rank):
            label = row.name
            if row.ip_address:
                label += f"   ({row.ip_address})"
            self.mgmt_interface_combo.addItem(label, row.name)

        if current:
            self.mgmt_interface_combo.setCurrentText(current)

    def set_connected_interface(self, name: str) -> None:
        """Record which interface our own session arrives on, to warn about it."""
        self._connected_via = name
        self._validate_management()

    def _load_management(self) -> None:
        self.service.load_management_config(self._current_mgmt_interface())

    def _on_mgmt_loaded(self, hostname: str, interface: str, ip: str,
                        mask: str, gateway: str) -> None:
        if hostname:
            self.hostname_edit.setText(hostname)
        if interface:
            index = self.mgmt_interface_combo.findData(interface)
            if index >= 0:
                self.mgmt_interface_combo.setCurrentIndex(index)
            else:
                self.mgmt_interface_combo.setCurrentText(interface)

        if ip == "dhcp":
            self.dhcp_check.setChecked(True)
        else:
            self.dhcp_check.setChecked(False)
            self.ip_edit.setText(ip)
            if mask:
                self.mask_edit.setText(mask)
        self.gateway_edit.setText(gateway)
        self.status_label.setText("Loaded the current configuration from the device.")
        self.status_label.setStyleSheet("")

    def _on_config_loaded(self, config: str) -> None:
        self.config_view.setPlainText(config)
        self.export_button.setEnabled(bool(config.strip()))
        lines = config.count("\n") + 1
        self.status_label.setText(f"Fetched the running configuration ({lines} lines).")
        self.status_label.setStyleSheet("")

    def _on_unsaved_changes(self, unsaved: bool) -> None:
        if unsaved:
            self.unsaved_label.setText("● Unsaved changes")
            self.unsaved_label.setStyleSheet(f"color: {Palette.WARNING};")
        else:
            self.unsaved_label.setText("● Saved")
            self.unsaved_label.setStyleSheet(f"color: {Palette.SUCCESS};")

    def _on_hostname_applied(self, hostname: str) -> None:
        self.fact_labels["hostname"].setText(hostname)
        self.hostname_changed.emit(hostname)

    def _on_busy(self, busy: bool) -> None:
        self.save_button.setText("Saving…" if busy else "Save Running Config")

    # ── validation ────────────────────────────────────────────────────────────

    def _validate_hostname(self) -> None:
        text = self.hostname_edit.text().strip()
        problem = parse_sys.validate_hostname(text) if text else ""
        self.hostname_error.setText(problem)
        self.hostname_edit.setProperty("invalid", bool(problem))
        self.hostname_edit.style().unpolish(self.hostname_edit)
        self.hostname_edit.style().polish(self.hostname_edit)

        current = self.fact_labels["hostname"].text()
        self.hostname_button.setEnabled(
            bool(text) and not problem and text != current)

    def _current_mgmt_interface(self) -> str:
        data = self.mgmt_interface_combo.currentData()
        if data:
            return str(data)
        return self.mgmt_interface_combo.currentText().split("   ")[0].strip()

    def _on_dhcp_toggled(self, enabled: bool) -> None:
        for widget in (self.ip_label, self.ip_edit, self.mask_label, self.mask_edit):
            widget.setEnabled(not enabled)
        self._validate_management()

    def _validate_management(self) -> None:
        interface = self._current_mgmt_interface()
        problems: list[str] = []

        if not interface:
            problems.append("Choose an interface.")

        if not self.dhcp_check.isChecked():
            ip, mask = self.ip_edit.text().strip(), self.mask_edit.text().strip()
            problem = parse_sys.validate_ipv4(ip)
            if problem:
                problems.append(problem)
            problem = parse_sys.validate_netmask(mask)
            if problem:
                problems.append(problem)

            gateway = self.gateway_edit.text().strip()
            if gateway:
                problem = parse_sys.validate_ipv4(gateway)
                if problem:
                    problems.append(problem)
                elif not problems and not parse_sys.same_subnet(ip, gateway, mask):
                    # Not fatal — a gateway off-subnet is occasionally deliberate —
                    # but it is nearly always a typo, so say something.
                    problems.append(
                        f"The gateway {gateway} is not inside the subnet defined "
                        f"by {ip}/{mask}.")

        self.mgmt_button.setEnabled(not problems)

        warning = ""
        if problems:
            warning = "• " + "\n• ".join(problems)
            colour = Palette.DANGER
        elif interface and interface == self._connected_via:
            warning = (f"⚠ You are connected to this device through {interface}. "
                       f"Changing its address will drop this session.")
            colour = Palette.WARNING
        if warning:
            self.mgmt_warning.setText(warning)
            self.mgmt_warning.setStyleSheet(f"color: {colour};")
            self.mgmt_warning.show()
        else:
            self.mgmt_warning.hide()

    # ── actions ───────────────────────────────────────────────────────────────

    def _apply_hostname(self) -> None:
        hostname = self.hostname_edit.text().strip()
        if not hostname or parse_sys.validate_hostname(hostname):
            return
        self.service.set_hostname(hostname)

    def _apply_management(self) -> None:
        interface = self._current_mgmt_interface()
        if not interface:
            return

        if interface == self._connected_via:
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setWindowTitle("Change the Address You Are Connected Through?")
            confirm.setText(
                f"You are reaching this device through {interface}.")
            confirm.setInformativeText(
                "Changing its address will immediately drop this session, and you "
                "will need to reconnect on the new address.\n\n"
                "This is safe over a console connection, but risky over SSH or "
                "Telnet.\n\nContinue?")
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return

        self.service.set_management_ip(
            interface,
            self.ip_edit.text().strip(),
            self.mask_edit.text().strip(),
            self.gateway_edit.text().strip(),
            use_dhcp=self.dhcp_check.isChecked(),
        )

    def _save_config(self) -> None:
        self.service.save_config()

    def _export_config(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        hostname = self.fact_labels["hostname"].text().replace("—", "device")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Running Config", f"{hostname}-running-config.txt",
            "Text files (*.txt);;Config files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.config_view.toPlainText())
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Save",
                                 f"The file could not be written.\n\n{exc}")
            return
        self.status_label.setText(f"Configuration saved to {path}")
        self.status_label.setStyleSheet(f"color: {Palette.SUCCESS};")
