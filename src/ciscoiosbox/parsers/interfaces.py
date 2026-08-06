"""Interface parsers.

The interface grid is a *merge* of two commands, because neither alone has
everything the user wants:

* ``show ip interface brief`` — every interface, its IP, and the admin state
  (only this command distinguishes "down" from "administratively down").
* ``show interfaces status`` — description, access VLAN, duplex, speed, media.
  Switch-only, and it omits L3-only interfaces such as Loopbacks.

:func:`merge_interface_data` combines them, keyed on the normalised name.
"""
from __future__ import annotations

import logging
import re

from ..core.models import InterfaceRow
from . import textfsm_parser as tfsm

log = logging.getLogger(__name__)


# ─── Name normalisation ───────────────────────────────────────────────────────

#: Abbreviation → canonical prefix. Longest keys must be tried first, hence the
#: explicit ordering rather than a plain dict iteration.
_EXPANSIONS: list[tuple[str, str]] = [
    ("twe", "TwentyFiveGigE"), ("tw", "TwoGigabitEthernet"),
    ("te", "TenGigabitEthernet"), ("fo", "FortyGigabitEthernet"),
    ("hu", "HundredGigE"), ("gi", "GigabitEthernet"), ("fa", "FastEthernet"),
    ("eth", "Ethernet"), ("et", "Ethernet"), ("po", "Port-channel"),
    ("lo", "Loopback"), ("vl", "Vlan"), ("tu", "Tunnel"), ("se", "Serial"),
    ("bd", "BDI"), ("nv", "nve"),
]

_NAME_SPLIT = re.compile(r"^([A-Za-z\-]+)\s*([\d/\.:]*)$")


def normalise_name(name: str) -> str:
    """Expand an abbreviated interface name to its canonical form.

    ``Gi1/0/1`` → ``GigabitEthernet1/0/1``. Used as the merge key so rows from
    two commands that spell the same port differently still line up.
    """
    name = (name or "").strip()
    if not name:
        return ""

    match = _NAME_SPLIT.match(name.replace(" ", ""))
    if not match:
        return name
    prefix, number = match.groups()
    lowered = prefix.lower()

    # Already canonical? Leave the caller's capitalisation alone.
    for _, canonical in _EXPANSIONS:
        if lowered == canonical.lower():
            return canonical + number

    for abbrev, canonical in _EXPANSIONS:
        if lowered.startswith(abbrev):
            return canonical + number
    return name


# ─── show ip interface brief ──────────────────────────────────────────────────

#: Interface  IP-Address  OK?  Method  Status  Protocol
_IP_BRIEF_ROW = re.compile(
    r"^(?P<intf>[A-Za-z][\w\-./:]*)\s+"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|unassigned|unnumbered.*?)\s+"
    r"(?P<ok>YES|NO)\s+"
    r"(?P<method>\S+)\s+"
    r"(?P<status>administratively down|up|down|reset|deleted)\s+"
    r"(?P<protocol>up|down)\s*$",
    re.I,
)


def parse_ip_interface_brief(output: str, platform: str = "cisco_ios") -> list[InterfaceRow]:
    """Parse ``show ip interface brief`` into rows."""
    rows = tfsm.parse("show ip interface brief", output, platform)
    if rows:
        parsed: list[InterfaceRow] = []
        for row in rows:
            name = tfsm.get(row, "interface", "intf", "port")
            if not name:
                continue
            parsed.append(InterfaceRow(
                name=normalise_name(name),
                ip_address=_clean_ip(tfsm.get(row, "ip_address", "ipaddr", "ip")),
                admin_status=tfsm.get(row, "status", "link_status"),
                oper_status=tfsm.get(row, "proto", "protocol", "protocol_status"),
            ))
        if parsed:
            return parsed

    return _parse_ip_brief_regex(output)


def _parse_ip_brief_regex(output: str) -> list[InterfaceRow]:
    """Regex fallback for ``show ip interface brief``."""
    result: list[InterfaceRow] = []
    for line in output.splitlines():
        line = line.rstrip()
        if not line or line.lower().startswith("interface "):
            continue
        match = _IP_BRIEF_ROW.match(line.strip())
        if not match:
            continue
        result.append(InterfaceRow(
            name=normalise_name(match.group("intf")),
            ip_address=_clean_ip(match.group("ip")),
            admin_status=match.group("status").strip(),
            oper_status=match.group("protocol").strip(),
        ))
    return result


def _clean_ip(value: str) -> str:
    """Blank out IOS's placeholder words so the column reads cleanly."""
    if not value:
        return ""
    lowered = value.strip().lower()
    if lowered in ("unassigned", "unnumbered", "n/a", "none"):
        return ""
    return value.strip()


# ─── show interfaces status ───────────────────────────────────────────────────

#: Header tokens in the order IOS prints them.
_STATUS_HEADERS = ("Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type")


def parse_interfaces_status(output: str, platform: str = "cisco_ios") -> list[InterfaceRow]:
    """Parse ``show interfaces status``.

    Descriptions contain spaces, so whitespace splitting is unreliable. We read
    the header row's column offsets and slice by position — the same way the
    device laid the table out.
    """
    rows = tfsm.parse("show interfaces status", output, platform)
    if rows:
        parsed: list[InterfaceRow] = []
        for row in rows:
            name = tfsm.get(row, "port", "interface", "intf")
            if not name:
                continue
            parsed.append(InterfaceRow(
                name=normalise_name(name),
                description=tfsm.get(row, "name", "description", "descrip"),
                oper_status=tfsm.get(row, "status", "link_status"),
                vlan=tfsm.get(row, "vlan", "vlan_id"),
                duplex=tfsm.get(row, "duplex"),
                speed=tfsm.get(row, "speed"),
                media_type=tfsm.get(row, "type", "media_type"),
            ))
        if parsed:
            return parsed

    return _parse_status_columns(output)


def _parse_status_columns(output: str) -> list[InterfaceRow]:
    """Column-offset fallback for ``show interfaces status``."""
    lines = output.splitlines()
    header_index = -1
    offsets: dict[str, int] = {}

    for index, line in enumerate(lines):
        # A valid header has Port and Status; Name is absent on some platforms.
        if "Port" in line and "Status" in line:
            positions = {h: line.find(h) for h in _STATUS_HEADERS}
            if positions["Port"] >= 0 and positions["Status"] > positions["Port"]:
                header_index = index
                offsets = {h: p for h, p in positions.items() if p >= 0}
                break

    if header_index < 0:
        return []

    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    result: list[InterfaceRow] = []

    for line in lines[header_index + 1:]:
        if not line.strip() or set(line.strip()) <= {"-"}:
            continue
        # Stop at a second header (paged output) or a prompt echo.
        if "Port" in line and "Status" in line:
            continue

        fields: dict[str, str] = {}
        for position, (header, start) in enumerate(ordered):
            end = ordered[position + 1][1] if position + 1 < len(ordered) else len(line)
            fields[header] = line[start:end].strip()

        port = fields.get("Port", "")
        if not port or " " in port:
            # Wrapped continuation line, not a real row.
            continue

        result.append(InterfaceRow(
            name=normalise_name(port),
            description=fields.get("Name", ""),
            oper_status=fields.get("Status", ""),
            vlan=fields.get("Vlan", ""),
            duplex=fields.get("Duplex", ""),
            speed=fields.get("Speed", ""),
            media_type=fields.get("Type", ""),
        ))
    return result


# ─── show interfaces description ──────────────────────────────────────────────

def parse_interface_descriptions(output: str) -> dict[str, str]:
    """Parse ``show interfaces description`` → {canonical name: description}.

    Used on routers, where ``show interfaces status`` does not exist.
    """
    lines = output.splitlines()
    result: dict[str, str] = {}
    desc_offset = -1

    for line in lines:
        if line.startswith("Interface") and "Description" in line:
            desc_offset = line.find("Description")
            continue
        if desc_offset < 0 or not line.strip():
            continue
        name = line.split()[0]
        if not re.match(r"^[A-Za-z][\w\-./:]*$", name):
            continue
        description = line[desc_offset:].strip() if len(line) > desc_offset else ""
        result[normalise_name(name)] = description
    return result


# ─── switchport mode ──────────────────────────────────────────────────────────

_TRUNK_HEADER = re.compile(r"^(Port|Name)\s+Mode\s+Encapsulation", re.I)


def parse_switchport_modes(output: str) -> dict[str, str]:
    """Parse ``show interfaces switchport`` → {canonical name: access|trunk|dynamic}.

    The grid shows this so the user can tell at a glance which ports are trunks
    without opening each one.
    """
    result: dict[str, str] = {}
    current = ""
    for line in output.splitlines():
        stripped = line.strip()
        match = re.match(r"^Name:\s*(\S+)", stripped)
        if match:
            current = normalise_name(match.group(1))
            continue
        match = re.match(r"^(?:Administrative|Operational) Mode:\s*(.+)$", stripped, re.I)
        if match and current:
            mode = match.group(1).strip().lower()
            # "static access", "trunk", "dynamic auto", "down" (shutdown trunk)
            if "trunk" in mode:
                result[current] = "trunk"
            elif "access" in mode:
                result[current] = "access"
            elif "dynamic" in mode:
                result.setdefault(current, "dynamic")
            # Operational Mode comes after Administrative Mode, so it wins.
    return result


# ─── merge ────────────────────────────────────────────────────────────────────

def merge_interface_data(
    ip_brief: list[InterfaceRow],
    status: list[InterfaceRow] | None = None,
    descriptions: dict[str, str] | None = None,
    modes: dict[str, str] | None = None,
) -> list[InterfaceRow]:
    """Combine the interface data sources into one row set.

    ``ip_brief`` is authoritative for which interfaces exist and their admin
    state. Everything else enriches those rows; a port appearing only in
    ``status`` (rare, but happens on some stacks) is appended rather than lost.
    """
    merged: dict[str, InterfaceRow] = {}

    for row in ip_brief:
        merged[row.name] = InterfaceRow(
            name=row.name,
            ip_address=row.ip_address,
            admin_status=row.admin_status,
            oper_status=row.oper_status,
        )

    for row in status or []:
        target = merged.get(row.name)
        if target is None:
            target = InterfaceRow(name=row.name)
            merged[row.name] = target
            # No ip-brief row, so infer admin state from the status column.
            target.admin_status = (
                "administratively down" if row.oper_status.lower() == "disabled" else "up")

        target.description = row.description or target.description
        target.vlan = row.vlan or target.vlan
        target.duplex = row.duplex or target.duplex
        target.speed = row.speed or target.speed
        target.media_type = row.media_type or target.media_type
        # `show interfaces status` is more precise than ip-brief's up/down:
        # it distinguishes connected / notconnect / err-disabled / disabled.
        if row.oper_status:
            target.oper_status = row.oper_status
        # "disabled" in the status column means shut down, which ip-brief on some
        # platforms reports merely as "down".
        if row.oper_status.lower() == "disabled":
            target.admin_status = "administratively down"

    for name, description in (descriptions or {}).items():
        target = merged.get(normalise_name(name))
        if target is not None and not target.description:
            target.description = description

    for name, mode in (modes or {}).items():
        target = merged.get(normalise_name(name))
        if target is not None:
            target.mode = mode
    # A trunk shows "trunk" in the Vlan column; use that when no switchport
    # data was collected.
    for row in merged.values():
        if not row.mode and row.vlan:
            row.mode = "trunk" if row.vlan.lower() == "trunk" else "access"

    return sorted(merged.values(), key=lambda r: _sort_key(r.name))


def _sort_key(name: str) -> tuple:
    """Natural sort: Gi1/0/2 before Gi1/0/10, and type groups stay together."""
    match = _NAME_SPLIT.match(name.replace(" ", ""))
    prefix = match.group(1) if match else name
    numbers = tuple(int(n) for n in re.findall(r"\d+", name))
    return (prefix.lower(), numbers, name)


def natural_sort_key(name: str) -> str:
    """Return a *string* sort key that orders interface names naturally.

    Qt's sort proxy compares whatever ``data()`` returns for the sort role, and
    a Python tuple does not survive that round trip usefully — it arrives as its
    ``repr``, where ``"(1, 0, 10)" < "(1, 0, 2)"`` and the ordering is wrong.

    Zero-padding each numeric component to a fixed width makes plain string
    comparison produce the same order as the tuple would:

        GigabitEthernet1/0/2   → "gigabitethernet|00001|00000|00002"
        GigabitEthernet1/0/10  → "gigabitethernet|00001|00000|00010"
    """
    match = _NAME_SPLIT.match(name.replace(" ", ""))
    prefix = (match.group(1) if match else name).lower()
    numbers = re.findall(r"\d+", name)
    padded = "|".join(f"{int(n):05d}" for n in numbers)
    return f"{prefix}|{padded}"


# ─── show interfaces <name> (rates + counters) ────────────────────────────────

_RATE_IN = re.compile(
    r"(\d+)\s*minute input rate\s+(\d+)\s*bits/sec,\s*(\d+)\s*packets/sec", re.I)
_RATE_OUT = re.compile(
    r"(\d+)\s*minute output rate\s+(\d+)\s*bits/sec,\s*(\d+)\s*packets/sec", re.I)
_BANDWIDTH = re.compile(r"BW\s+(\d+)\s*(Kbit|Mbit|Gbit)?", re.I)
_IN_OCTETS = re.compile(r"(\d+)\s+packets input,\s*(\d+)\s+bytes", re.I)
_OUT_OCTETS = re.compile(r"(\d+)\s+packets output,\s*(\d+)\s+bytes", re.I)


def parse_interface_rates(output: str) -> dict[str, float | int]:
    """Extract throughput from ``show interfaces <name>``.

    IOS reports a 5-minute exponentially-weighted average by default, so these
    numbers lag a sudden burst. The monitor service prefers SNMP counter deltas
    when SNMP is configured, and uses this as the CLI-only fallback.
    """
    data: dict[str, float | int] = {
        "rx_bps": 0.0, "tx_bps": 0.0, "rx_pps": 0.0, "tx_pps": 0.0,
        "bandwidth_bps": 0.0, "rx_octets": 0, "tx_octets": 0,
    }

    match = _RATE_IN.search(output)
    if match:
        data["rx_bps"] = float(match.group(2))
        data["rx_pps"] = float(match.group(3))
    match = _RATE_OUT.search(output)
    if match:
        data["tx_bps"] = float(match.group(2))
        data["tx_pps"] = float(match.group(3))

    match = _BANDWIDTH.search(output)
    if match:
        # The BW field is in Kbit/sec unless a unit is spelled out.
        value = float(match.group(1))
        unit = (match.group(2) or "Kbit").lower()
        multiplier = {"kbit": 1e3, "mbit": 1e6, "gbit": 1e9}.get(unit, 1e3)
        data["bandwidth_bps"] = value * multiplier

    match = _IN_OCTETS.search(output)
    if match:
        data["rx_octets"] = int(match.group(2))
    match = _OUT_OCTETS.search(output)
    if match:
        data["tx_octets"] = int(match.group(2))

    return data
