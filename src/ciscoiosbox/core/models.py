"""Plain dataclasses shared across every layer.

These types are deliberately free of Qt and netmiko imports so parsers stay
unit-testable and the same structures can travel from a background thread into
the UI without conversion.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ConnectionType(str, Enum):
    """How we reach the device. Values double as netmiko device-type suffixes."""

    SSH = "ssh"
    TELNET = "telnet"
    SERIAL = "serial"

    @property
    def label(self) -> str:
        return {"ssh": "SSH", "telnet": "Telnet", "serial": "Serial / Console"}[self.value]

    @property
    def default_port(self) -> int:
        return {"ssh": 22, "telnet": 23, "serial": 0}[self.value]


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


# ─── Session profiles ─────────────────────────────────────────────────────────

@dataclass
class SerialSettings:
    """pyserial parameters, used only when ``ConnectionType.SERIAL``."""

    port: str = ""                 # e.g. "COM3" on Windows, "/dev/tty.usbserial" on macOS
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"              # N / E / O
    stopbits: int = 1
    # Cisco consoles use no flow control; enabling it is a classic hang cause.
    xonxoff: bool = False
    rtscts: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SerialSettings:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class SnmpSettings:
    """SNMP polling config. v2c uses ``community``; v3 uses the auth/priv fields."""

    enabled: bool = False
    version: str = "2c"            # "2c" or "3"
    port: int = 161
    community: str = ""            # v2c only — stored in the secret vault
    # v3
    username: str = ""
    auth_protocol: str = "SHA"     # MD5 / SHA / SHA224 / SHA256 / SHA384 / SHA512
    auth_key: str = ""             # stored in the secret vault
    priv_protocol: str = "AES128"  # DES / 3DES / AES128 / AES192 / AES256
    priv_key: str = ""             # stored in the secret vault
    timeout: float = 2.0
    retries: int = 1

    def to_dict(self) -> dict[str, Any]:
        # Secrets are stripped here; session_store persists them via CredentialStore.
        data = asdict(self)
        for secret in ("community", "auth_key", "priv_key"):
            data.pop(secret, None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnmpSettings:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class DeviceProfile:
    """A saved device the user can one-click connect to."""

    name: str = "New Device"
    host: str = ""
    connection_type: ConnectionType = ConnectionType.SSH
    port: int = 22
    username: str = ""
    # Secrets live in the vault, never in the profile JSON. They are populated
    # in memory only for the lifetime of a connection attempt.
    password: str = field(default="", repr=False, compare=False)
    enable_password: str = field(default="", repr=False, compare=False)
    save_password: bool = True

    serial: SerialSettings = field(default_factory=SerialSettings)
    snmp: SnmpSettings = field(default_factory=SnmpSettings)

    # netmiko tuning
    device_type_base: str = "cisco_ios"   # cisco_ios | cisco_xe | cisco_nxos | cisco_s300
    conn_timeout: float = 12.0
    read_timeout: float = 25.0
    global_delay_factor: float = 1.0
    fast_cli: bool = True

    group: str = ""                       # optional folder label in the session list
    notes: str = ""
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # ── netmiko wiring ────────────────────────────────────────────────────────

    @property
    def netmiko_device_type(self) -> str:
        """Map our connection type onto netmiko's device-type naming scheme."""
        if self.connection_type is ConnectionType.TELNET:
            return f"{self.device_type_base}_telnet"
        if self.connection_type is ConnectionType.SERIAL:
            return f"{self.device_type_base}_serial"
        return self.device_type_base

    @property
    def display_target(self) -> str:
        """Human-readable destination, used in window titles and the session list."""
        if self.connection_type is ConnectionType.SERIAL:
            return f"{self.serial.port or '(no port)'} @ {self.serial.baudrate}"
        return f"{self.host}:{self.port}"

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means good to go."""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("Profile name cannot be empty.")
        if self.connection_type is ConnectionType.SERIAL:
            if not self.serial.port.strip():
                problems.append("Select a serial port.")
            if self.serial.baudrate <= 0:
                problems.append("Baud rate must be a positive number.")
        else:
            if not self.host.strip():
                problems.append("Host / IP address cannot be empty.")
            if not 1 <= self.port <= 65535:
                problems.append("Port must be between 1 and 65535.")
            # Serial consoles legitimately have no username; network logins need one.
            if self.connection_type is ConnectionType.SSH and not self.username.strip():
                problems.append("SSH requires a username.")
        if self.snmp.enabled:
            if self.snmp.version == "2c" and not self.snmp.community:
                problems.append("SNMP v2c requires a community string.")
            if self.snmp.version == "3" and not self.snmp.username:
                problems.append("SNMP v3 requires a username.")
        return problems

    # ── persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialise for on-disk storage. Secrets are intentionally omitted."""
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "host": self.host,
            "connection_type": self.connection_type.value,
            "port": self.port,
            "username": self.username,
            "save_password": self.save_password,
            "serial": self.serial.to_dict(),
            "snmp": self.snmp.to_dict(),
            "device_type_base": self.device_type_base,
            "conn_timeout": self.conn_timeout,
            "read_timeout": self.read_timeout,
            "global_delay_factor": self.global_delay_factor,
            "fast_cli": self.fast_cli,
            "group": self.group,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceProfile:
        return cls(
            profile_id=data.get("profile_id") or uuid.uuid4().hex,
            name=data.get("name", "Unnamed"),
            host=data.get("host", ""),
            connection_type=ConnectionType(data.get("connection_type", "ssh")),
            port=int(data.get("port", 22)),
            username=data.get("username", ""),
            save_password=bool(data.get("save_password", True)),
            serial=SerialSettings.from_dict(data.get("serial", {})),
            snmp=SnmpSettings.from_dict(data.get("snmp", {})),
            device_type_base=data.get("device_type_base", "cisco_ios"),
            conn_timeout=float(data.get("conn_timeout", 12.0)),
            read_timeout=float(data.get("read_timeout", 25.0)),
            global_delay_factor=float(data.get("global_delay_factor", 1.0)),
            fast_cli=bool(data.get("fast_cli", True)),
            group=data.get("group", ""),
            notes=data.get("notes", ""),
        )

    def copy_with_secrets(self, password: str, enable_password: str) -> DeviceProfile:
        """Return a shallow clone carrying secrets, for handing to a transport."""
        import copy

        clone = copy.deepcopy(self)
        clone.password = password
        clone.enable_password = enable_password
        return clone


# ─── Device data ──────────────────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Result of the post-login fact-gathering pass."""

    hostname: str = ""
    model: str = ""
    version: str = ""
    serial_number: str = ""
    uptime: str = ""
    image: str = ""
    in_enable_mode: bool = False


@dataclass
class InterfaceRow:
    """One row of the interface grid.

    Merges ``show ip interface brief`` (addressing, admin state) with
    ``show interfaces status`` (description, vlan, duplex, speed, media).
    """

    name: str = ""
    ip_address: str = ""
    # "up"/"down"/"administratively down" from `show ip int brief`
    admin_status: str = ""
    oper_status: str = ""          # protocol / line status
    description: str = ""
    vlan: str = ""                 # access vlan or "trunk"
    duplex: str = ""
    speed: str = ""
    media_type: str = ""
    mode: str = ""                 # access / trunk / dynamic — from switchport info

    @property
    def is_shutdown(self) -> bool:
        """True when the port is administratively disabled."""
        return "administratively down" in self.admin_status.lower() or (
            self.admin_status.lower() in ("disabled", "admin down")
        )

    @property
    def is_up(self) -> bool:
        return self.oper_status.lower() in ("up", "connected") or (
            self.admin_status.lower() == "up" and "down" not in self.oper_status.lower()
        )

    @property
    def short_name(self) -> str:
        """Abbreviate ``GigabitEthernet1/0/1`` → ``Gi1/0/1`` for tight columns."""
        abbreviations = [
            ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
            ("FastEthernet", "Fa"), ("FortyGigabitEthernet", "Fo"),
            ("TwentyFiveGigE", "Twe"), ("HundredGigE", "Hu"),
            ("Ethernet", "Et"), ("Port-channel", "Po"), ("Loopback", "Lo"),
            ("Vlan", "Vl"), ("Tunnel", "Tu"), ("Serial", "Se"),
        ]
        for long, short in abbreviations:
            if self.name.startswith(long):
                return short + self.name[len(long):]
        return self.name


@dataclass
class Vlan:
    """One VLAN from ``show vlan brief``."""

    vlan_id: int = 0
    name: str = ""
    status: str = ""               # active / act/lshut / suspended
    interfaces: list[str] = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        """VLAN 1 and the 1002-1005 FDDI/TokenRing relics cannot be deleted."""
        return self.vlan_id == 1 or 1002 <= self.vlan_id <= 1005


@dataclass
class ResourceSample:
    """A single CPU / memory reading, used to feed the monitoring graphs."""

    timestamp: float = 0.0
    cpu_5sec: float = 0.0
    cpu_1min: float = 0.0
    cpu_5min: float = 0.0
    mem_used_bytes: int = 0
    mem_free_bytes: int = 0

    @property
    def mem_total_bytes(self) -> int:
        return self.mem_used_bytes + self.mem_free_bytes

    @property
    def mem_used_percent(self) -> float:
        total = self.mem_total_bytes
        return (self.mem_used_bytes / total * 100.0) if total else 0.0


@dataclass
class TrafficSample:
    """Interface throughput at a point in time, in bits per second."""

    timestamp: float = 0.0
    interface: str = ""
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_pps: float = 0.0
    tx_pps: float = 0.0
    # Raw counters, when sourced from SNMP — lets the service compute deltas.
    rx_octets: int | None = None
    tx_octets: int | None = None
    bandwidth_bps: float = 0.0     # interface nominal speed, for % utilisation

    def utilisation(self) -> tuple[float, float]:
        """Return (rx%, tx%) of nominal bandwidth, clamped to 0-100."""
        if self.bandwidth_bps <= 0:
            return 0.0, 0.0
        return (
            min(100.0, self.rx_bps / self.bandwidth_bps * 100.0),
            min(100.0, self.tx_bps / self.bandwidth_bps * 100.0),
        )
