"""Device profile editor.

Tabs mirror the mental model of connecting to a device: where it is, who you
log in as, how to monitor it, and the tuning you rarely touch.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.models import ConnectionType, DeviceProfile
from ..core.snmp import snmp_available
from .theme import Palette

log = logging.getLogger(__name__)

#: Common console speeds; 9600 is the Cisco default.
BAUD_RATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]

#: netmiko base device types this app has been exercised against.
PLATFORMS = [
    ("cisco_ios", "Cisco IOS / IOS-XE (most switches & routers)"),
    ("cisco_xe", "Cisco IOS-XE (explicit)"),
    ("cisco_nxos", "Cisco NX-OS (Nexus)"),
    ("cisco_xr", "Cisco IOS-XR"),
    ("cisco_s300", "Cisco Small Business (SG300 etc.)"),
]


def list_serial_ports() -> list[tuple[str, str]]:
    """Enumerate serial ports as (device, human description)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        log.warning("pyserial is not installed; serial ports cannot be listed.")
        return []

    ports = []
    for port in list_ports.comports():
        description = port.description or "Serial port"
        if port.manufacturer and port.manufacturer not in description:
            description = f"{description} — {port.manufacturer}"
        ports.append((port.device, description))
    return sorted(ports, key=lambda p: p[0])


class SessionDialog(QDialog):
    """Create or edit a :class:`DeviceProfile`."""

    #: Emitted when the user asks to save *and* connect immediately.
    connect_requested = Signal(object)

    def __init__(self, profile: DeviceProfile | None = None,
                 parent: QWidget | None = None, *, allow_connect: bool = True) -> None:
        super().__init__(parent)
        self.is_new = profile is None
        self.profile = profile or DeviceProfile()
        self._allow_connect = allow_connect

        self.setWindowTitle("New Session" if self.is_new else f"Edit — {self.profile.name}")
        self.setMinimumWidth(560)
        self.setModal(True)

        self._build_ui()
        self._load_profile()
        self._update_visibility()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_connection_tab(), "Connection")
        self.tabs.addTab(self._build_credentials_tab(), "Credentials")
        self.tabs.addTab(self._build_snmp_tab(), "SNMP")
        self.tabs.addTab(self._build_advanced_tab(), "Advanced")
        layout.addWidget(self.tabs)

        self.error_label = QLabel()
        self.error_label.setProperty("error", True)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("accent", True)
        if self._allow_connect:
            self.connect_button = buttons.addButton(
                "Save && Connect", QDialogButtonBox.ButtonRole.ApplyRole)
            self.connect_button.clicked.connect(self._on_save_and_connect)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_connection_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Core Switch 01")
        form.addRow("Profile name", self.name_edit)

        self.group_edit = QLineEdit()
        self.group_edit.setPlaceholderText("Optional — groups sessions in the list")
        form.addRow("Group", self.group_edit)
        layout.addWidget(identity)

        transport = QGroupBox("Transport")
        transport_form = QFormLayout(transport)

        self.type_combo = QComboBox()
        for connection_type in ConnectionType:
            # Store the enum's plain value, not the member: PySide6 marshals
            # str-subclass enums into a QVariant as a plain str, so storing the
            # member would hand _current_type() a str and break the .value /
            # identity lookups.
            self.type_combo.addItem(connection_type.label, connection_type.value)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        transport_form.addRow("Connection type", self.type_combo)

        # — network fields —
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("192.168.1.1 or switch.example.com")
        self.host_label = QLabel("Host / IP")
        transport_form.addRow(self.host_label, self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.port_label = QLabel("Port")
        transport_form.addRow(self.port_label, self.port_spin)

        # — serial fields —
        serial_row = QWidget()
        serial_layout = QHBoxLayout(serial_row)
        serial_layout.setContentsMargins(0, 0, 0, 0)
        self.serial_port_combo = QComboBox()
        self.serial_port_combo.setEditable(True)
        self.serial_port_combo.setMinimumWidth(220)
        refresh_ports = QPushButton("Rescan")
        refresh_ports.setToolTip("Re-enumerate the serial ports attached to this computer")
        refresh_ports.clicked.connect(self._refresh_serial_ports)
        serial_layout.addWidget(self.serial_port_combo, 1)
        serial_layout.addWidget(refresh_ports)
        self.serial_port_label = QLabel("Serial port")
        transport_form.addRow(self.serial_port_label, serial_row)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText("9600")
        self.baud_label = QLabel("Baud rate")
        transport_form.addRow(self.baud_label, self.baud_combo)

        self.serial_format_combo = QComboBox()
        self.serial_format_combo.addItems(["8-N-1 (standard)", "7-E-1", "7-O-1", "8-N-2"])
        self.serial_format_label = QLabel("Data format")
        transport_form.addRow(self.serial_format_label, self.serial_format_combo)

        layout.addWidget(transport)

        self.transport_hint = QLabel()
        self.transport_hint.setProperty("muted", True)
        self.transport_hint.setWordWrap(True)
        layout.addWidget(self.transport_hint)

        layout.addStretch(1)
        self._refresh_serial_ports()
        return page

    def _build_credentials_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        login = QGroupBox("Login")
        form = QFormLayout(login)

        self.username_edit = QLineEdit()
        form.addRow("Username", self.username_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        password_row = self._with_reveal(self.password_edit)
        form.addRow("Password", password_row)

        self.enable_edit = QLineEdit()
        self.enable_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_edit.setPlaceholderText("Leave blank if none is set")
        form.addRow("Enable secret", self._with_reveal(self.enable_edit))

        layout.addWidget(login)

        storage = QGroupBox("Storage")
        storage_layout = QVBoxLayout(storage)
        self.save_password_check = QCheckBox("Remember these credentials")
        self.save_password_check.setChecked(True)
        storage_layout.addWidget(self.save_password_check)

        self.storage_note = QLabel()
        self.storage_note.setProperty("muted", True)
        self.storage_note.setWordWrap(True)
        storage_layout.addWidget(self.storage_note)
        layout.addWidget(storage)

        note = QLabel(
            "Enable secret is required to change configuration. Without it you "
            "can still browse interfaces, VLANs and monitoring data.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)
        return page

    def _build_snmp_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.snmp_enable_check = QCheckBox(
            "Use SNMP for monitoring (recommended — far lighter than CLI polling)")
        self.snmp_enable_check.toggled.connect(self._update_visibility)
        layout.addWidget(self.snmp_enable_check)

        if not snmp_available():
            unavailable = QLabel(
                "pysnmp is not installed, so SNMP polling is unavailable. "
                "Install it with 'pip install pysnmp' to enable this tab. "
                "Monitoring will fall back to CLI polling.")
            unavailable.setWordWrap(True)
            unavailable.setStyleSheet(f"color: {Palette.WARNING};")
            layout.addWidget(unavailable)
            self.snmp_enable_check.setEnabled(False)

        self.snmp_box = QGroupBox("SNMP settings")
        form = QFormLayout(self.snmp_box)

        self.snmp_version_combo = QComboBox()
        self.snmp_version_combo.addItem("v2c (community string)", "2c")
        self.snmp_version_combo.addItem("v3 (user-based security)", "3")
        self.snmp_version_combo.currentIndexChanged.connect(self._update_visibility)
        form.addRow("Version", self.snmp_version_combo)

        self.snmp_port_spin = QSpinBox()
        self.snmp_port_spin.setRange(1, 65535)
        self.snmp_port_spin.setValue(161)
        form.addRow("Port", self.snmp_port_spin)

        # — v2c —
        self.snmp_community_edit = QLineEdit()
        self.snmp_community_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.snmp_community_edit.setPlaceholderText("e.g. public (read-only)")
        self.snmp_community_row = self._with_reveal(self.snmp_community_edit)
        self.snmp_community_label = QLabel("Community")
        form.addRow(self.snmp_community_label, self.snmp_community_row)

        # — v3 —
        self.snmp_user_edit = QLineEdit()
        self.snmp_user_label = QLabel("Username")
        form.addRow(self.snmp_user_label, self.snmp_user_edit)

        self.snmp_auth_proto_combo = QComboBox()
        self.snmp_auth_proto_combo.addItems(
            ["SHA", "MD5", "SHA224", "SHA256", "SHA384", "SHA512"])
        self.snmp_auth_proto_label = QLabel("Auth protocol")
        form.addRow(self.snmp_auth_proto_label, self.snmp_auth_proto_combo)

        self.snmp_auth_key_edit = QLineEdit()
        self.snmp_auth_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.snmp_auth_key_label = QLabel("Auth key")
        form.addRow(self.snmp_auth_key_label, self._with_reveal(self.snmp_auth_key_edit))

        self.snmp_priv_proto_combo = QComboBox()
        self.snmp_priv_proto_combo.addItems(["AES128", "AES192", "AES256", "DES"])
        self.snmp_priv_proto_label = QLabel("Privacy protocol")
        form.addRow(self.snmp_priv_proto_label, self.snmp_priv_proto_combo)

        self.snmp_priv_key_edit = QLineEdit()
        self.snmp_priv_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.snmp_priv_key_label = QLabel("Privacy key")
        form.addRow(self.snmp_priv_key_label, self._with_reveal(self.snmp_priv_key_edit))

        layout.addWidget(self.snmp_box)

        hint = QLabel(
            "Leaving the auth or privacy key blank selects the corresponding "
            "no-auth / no-privacy security level.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch(1)
        return page

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        platform_box = QGroupBox("Platform")
        platform_form = QFormLayout(platform_box)
        self.platform_combo = QComboBox()
        for value, label in PLATFORMS:
            self.platform_combo.addItem(label, value)
        platform_form.addRow("Device type", self.platform_combo)
        layout.addWidget(platform_box)

        timing = QGroupBox("Timing")
        timing_form = QFormLayout(timing)

        self.conn_timeout_spin = QDoubleSpinBox()
        self.conn_timeout_spin.setRange(3.0, 120.0)
        self.conn_timeout_spin.setSuffix(" s")
        self.conn_timeout_spin.setValue(12.0)
        timing_form.addRow("Connect timeout", self.conn_timeout_spin)

        self.read_timeout_spin = QDoubleSpinBox()
        self.read_timeout_spin.setRange(5.0, 300.0)
        self.read_timeout_spin.setSuffix(" s")
        self.read_timeout_spin.setValue(25.0)
        timing_form.addRow("Command timeout", self.read_timeout_spin)

        self.delay_factor_spin = QDoubleSpinBox()
        self.delay_factor_spin.setRange(0.1, 10.0)
        self.delay_factor_spin.setSingleStep(0.5)
        self.delay_factor_spin.setValue(1.0)
        self.delay_factor_spin.setToolTip(
            "Multiplies every internal wait. Raise it for slow or congested "
            "links where commands time out prematurely.")
        timing_form.addRow("Delay factor", self.delay_factor_spin)

        self.fast_cli_check = QCheckBox("Fast CLI mode")
        self.fast_cli_check.setChecked(True)
        self.fast_cli_check.setToolTip(
            "Reduces inter-command delays. Turn this off if output arrives "
            "truncated or interleaved.")
        timing_form.addRow("", self.fast_cli_check)
        layout.addWidget(timing)

        notes_box = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_box)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Free-text notes about this device…")
        self.notes_edit.setMaximumHeight(90)
        notes_layout.addWidget(self.notes_edit)
        layout.addWidget(notes_box)

        layout.addStretch(1)
        return page

    @staticmethod
    def _with_reveal(line_edit: QLineEdit) -> QWidget:
        """Wrap a password field with a show/hide toggle."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(line_edit, 1)

        toggle = QPushButton("👁")
        toggle.setCheckable(True)
        toggle.setFixedWidth(34)
        toggle.setToolTip("Show or hide this value")
        toggle.toggled.connect(
            lambda shown: line_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        layout.addWidget(toggle)
        return container

    # ── dynamic behaviour ─────────────────────────────────────────────────────

    def _refresh_serial_ports(self) -> None:
        current = self.serial_port_combo.currentText()
        self.serial_port_combo.clear()
        ports = list_serial_ports()
        for device, description in ports:
            self.serial_port_combo.addItem(f"{device} — {description}", device)
        if not ports:
            self.serial_port_combo.addItem("No serial ports detected", "")
        if current:
            self.serial_port_combo.setCurrentText(current)

    def _selected_serial_port(self) -> str:
        """Extract the device path, whether picked from the list or typed."""
        data = self.serial_port_combo.currentData()
        if data:
            return str(data)
        text = self.serial_port_combo.currentText().strip()
        if text.startswith("No serial ports"):
            return ""
        # The user may have typed a bare path, or edited a "dev — description" entry.
        return text.split(" — ")[0].strip()

    def _current_type(self) -> ConnectionType:
        data = self.type_combo.currentData()
        if data:
            try:
                return ConnectionType(data)
            except ValueError:
                log.warning("Unknown connection type %r in the dialog", data)
        return ConnectionType.SSH

    def _on_type_changed(self) -> None:
        connection_type = self._current_type()
        if connection_type is not ConnectionType.SERIAL:
            # Move the port to the new protocol's default, unless the user has
            # deliberately set something non-standard.
            current = self.port_spin.value()
            if current in (22, 23):
                self.port_spin.setValue(connection_type.default_port)
        self._update_visibility()

    def _update_visibility(self) -> None:
        connection_type = self._current_type()
        is_serial = connection_type is ConnectionType.SERIAL

        for widget in (self.host_label, self.host_edit, self.port_label, self.port_spin):
            widget.setVisible(not is_serial)
        for widget in (self.serial_port_label, self.serial_port_combo.parentWidget(),
                       self.baud_label, self.baud_combo,
                       self.serial_format_label, self.serial_format_combo):
            widget.setVisible(is_serial)

        hints = {
            ConnectionType.SSH: "Encrypted. The device needs 'ip ssh' configured "
                                "and a local user or AAA login.",
            ConnectionType.TELNET: "Unencrypted — credentials travel in clear text. "
                                   "Use it only on an isolated management network.",
            ConnectionType.SERIAL: "Direct console access. Works even when the "
                                   "device has no IP configuration.",
        }
        self.transport_hint.setText(hints[connection_type])
        colour = Palette.WARNING if connection_type is ConnectionType.TELNET \
            else Palette.TEXT_MUTED
        self.transport_hint.setStyleSheet(f"color: {colour};")

        # SNMP needs an IP address, which a console session does not have.
        snmp_possible = not is_serial and snmp_available()
        self.snmp_enable_check.setEnabled(snmp_possible)
        if is_serial and self.snmp_enable_check.isChecked():
            self.snmp_enable_check.setChecked(False)

        snmp_on = self.snmp_enable_check.isChecked() and snmp_possible
        self.snmp_box.setEnabled(snmp_on)
        is_v3 = self.snmp_version_combo.currentData() == "3"
        for widget in (self.snmp_community_label, self.snmp_community_row):
            widget.setVisible(not is_v3)
        for widget in (self.snmp_user_label, self.snmp_user_edit,
                       self.snmp_auth_proto_label, self.snmp_auth_proto_combo,
                       self.snmp_auth_key_label, self.snmp_auth_key_edit.parentWidget(),
                       self.snmp_priv_proto_label, self.snmp_priv_proto_combo,
                       self.snmp_priv_key_label, self.snmp_priv_key_edit.parentWidget()):
            widget.setVisible(is_v3)

    def set_storage_note(self, text: str) -> None:
        """Tell the user where saved credentials will actually be stored."""
        self.storage_note.setText(text)

    # ── data binding ──────────────────────────────────────────────────────────

    def _load_profile(self) -> None:
        profile = self.profile
        self.name_edit.setText(profile.name)
        self.group_edit.setText(profile.group)

        index = self.type_combo.findData(profile.connection_type.value)
        self.type_combo.setCurrentIndex(max(0, index))

        self.host_edit.setText(profile.host)
        self.port_spin.setValue(profile.port or profile.connection_type.default_port)

        if profile.serial.port:
            self.serial_port_combo.setCurrentText(profile.serial.port)
        self.baud_combo.setCurrentText(str(profile.serial.baudrate))
        self.serial_format_combo.setCurrentIndex(
            self._format_index(profile.serial.bytesize, profile.serial.parity,
                               profile.serial.stopbits))

        self.username_edit.setText(profile.username)
        self.password_edit.setText(profile.password)
        self.enable_edit.setText(profile.enable_password)
        self.save_password_check.setChecked(profile.save_password)

        snmp = profile.snmp
        self.snmp_enable_check.setChecked(snmp.enabled)
        self.snmp_version_combo.setCurrentIndex(
            max(0, self.snmp_version_combo.findData(snmp.version)))
        self.snmp_port_spin.setValue(snmp.port)
        self.snmp_community_edit.setText(snmp.community)
        self.snmp_user_edit.setText(snmp.username)
        self.snmp_auth_proto_combo.setCurrentText(snmp.auth_protocol)
        self.snmp_auth_key_edit.setText(snmp.auth_key)
        self.snmp_priv_proto_combo.setCurrentText(snmp.priv_protocol)
        self.snmp_priv_key_edit.setText(snmp.priv_key)

        platform_index = self.platform_combo.findData(profile.device_type_base)
        self.platform_combo.setCurrentIndex(max(0, platform_index))
        self.conn_timeout_spin.setValue(profile.conn_timeout)
        self.read_timeout_spin.setValue(profile.read_timeout)
        self.delay_factor_spin.setValue(profile.global_delay_factor)
        self.fast_cli_check.setChecked(profile.fast_cli)
        self.notes_edit.setPlainText(profile.notes)

    @staticmethod
    def _format_index(bytesize: int, parity: str, stopbits: int) -> int:
        formats = [(8, "N", 1), (7, "E", 1), (7, "O", 1), (8, "N", 2)]
        try:
            return formats.index((bytesize, parity, stopbits))
        except ValueError:
            return 0

    def _collect(self) -> DeviceProfile:
        """Read every field back into the profile object."""
        profile = self.profile
        profile.name = self.name_edit.text().strip()
        profile.group = self.group_edit.text().strip()
        profile.connection_type = self._current_type()
        profile.host = self.host_edit.text().strip()
        profile.port = self.port_spin.value()

        profile.serial.port = self._selected_serial_port()
        try:
            profile.serial.baudrate = int(self.baud_combo.currentText().strip())
        except ValueError:
            profile.serial.baudrate = 9600
        bytesize, parity, stopbits = [
            (8, "N", 1), (7, "E", 1), (7, "O", 1), (8, "N", 2),
        ][self.serial_format_combo.currentIndex()]
        profile.serial.bytesize = bytesize
        profile.serial.parity = parity
        profile.serial.stopbits = stopbits

        profile.username = self.username_edit.text().strip()
        profile.password = self.password_edit.text()
        profile.enable_password = self.enable_edit.text()
        profile.save_password = self.save_password_check.isChecked()

        profile.snmp.enabled = self.snmp_enable_check.isChecked()
        profile.snmp.version = self.snmp_version_combo.currentData() or "2c"
        profile.snmp.port = self.snmp_port_spin.value()
        profile.snmp.community = self.snmp_community_edit.text()
        profile.snmp.username = self.snmp_user_edit.text().strip()
        profile.snmp.auth_protocol = self.snmp_auth_proto_combo.currentText()
        profile.snmp.auth_key = self.snmp_auth_key_edit.text()
        profile.snmp.priv_protocol = self.snmp_priv_proto_combo.currentText()
        profile.snmp.priv_key = self.snmp_priv_key_edit.text()

        profile.device_type_base = self.platform_combo.currentData() or "cisco_ios"
        profile.conn_timeout = self.conn_timeout_spin.value()
        profile.read_timeout = self.read_timeout_spin.value()
        profile.global_delay_factor = self.delay_factor_spin.value()
        profile.fast_cli = self.fast_cli_check.isChecked()
        profile.notes = self.notes_edit.toPlainText().strip()

        # Default the profile name to the target so the list is never full of
        # entries called "New Device".
        if not profile.name:
            profile.name = profile.display_target
        return profile

    # ── validation & result ───────────────────────────────────────────────────

    def _validate(self) -> bool:
        profile = self._collect()
        problems = profile.validate()
        if problems:
            self.error_label.setText("• " + "\n• ".join(problems))
            self.error_label.show()
            # Jump to the tab holding the first problem so the fix is one click away.
            first = problems[0].lower()
            if "snmp" in first:
                self.tabs.setCurrentIndex(2)
            elif "username" in first and "snmp" not in first:
                self.tabs.setCurrentIndex(1)
            else:
                self.tabs.setCurrentIndex(0)
            return False
        self.error_label.hide()
        return True

    def _on_save(self) -> None:
        if self._validate():
            self.accept()

    def _on_save_and_connect(self) -> None:
        if self._validate():
            self.connect_requested.emit(self.profile)
            self.accept()

    def result_profile(self) -> DeviceProfile:
        """The edited profile. Only meaningful after the dialog was accepted."""
        return self.profile
