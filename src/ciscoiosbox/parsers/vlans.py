"""VLAN parsers."""
from __future__ import annotations

import re

from ..core.models import Vlan
from . import textfsm_parser as tfsm
from .interfaces import normalise_name

#: ``1    default    active    Gi1/0/1, Gi1/0/2``
_VLAN_ROW = re.compile(
    r"^(?P<id>\d{1,4})\s+"
    r"(?P<name>\S+)\s+"
    r"(?P<status>active|suspended|act/lshut|sus/lshut|act/unsup)\s*"
    r"(?P<ports>.*)$",
    re.I,
)


def parse_show_vlan_brief(output: str, platform: str = "cisco_ios") -> list[Vlan]:
    """Parse ``show vlan brief``.

    The port list wraps onto continuation lines that carry no VLAN id, so the
    regex path tracks the last-seen VLAN and appends to it.
    """
    rows = tfsm.parse("show vlan brief", output, platform)
    if rows:
        parsed: list[Vlan] = []
        for row in rows:
            raw_id = tfsm.get(row, "vlan_id", "vlan", "id")
            if not raw_id.isdigit():
                continue
            ports = row.get("interfaces") or row.get("ports") or []
            if isinstance(ports, str):
                ports = [p.strip() for p in ports.split(",") if p.strip()]
            parsed.append(Vlan(
                vlan_id=int(raw_id),
                name=tfsm.get(row, "vlan_name", "name"),
                status=tfsm.get(row, "status", "state"),
                interfaces=[normalise_name(p) for p in ports],
            ))
        if parsed:
            return sorted(parsed, key=lambda v: v.vlan_id)

    return _parse_vlan_brief_regex(output)


def _parse_vlan_brief_regex(output: str) -> list[Vlan]:
    vlans: list[Vlan] = []
    current: Vlan | None = None

    for line in output.splitlines():
        if not line.strip() or set(line.strip()) <= {"-"}:
            continue
        if re.match(r"^\s*VLAN\s+Name\s+Status", line, re.I):
            continue

        match = _VLAN_ROW.match(line.strip())
        if match:
            current = Vlan(
                vlan_id=int(match.group("id")),
                name=match.group("name"),
                status=match.group("status").lower(),
                interfaces=_split_ports(match.group("ports")),
            )
            vlans.append(current)
            continue

        # A line that starts with whitespace and holds only port names is a
        # continuation of the previous VLAN's port list.
        if current is not None and line.startswith((" ", "\t")):
            extra = _split_ports(line)
            if extra:
                current.interfaces.extend(extra)

    return sorted(vlans, key=lambda v: v.vlan_id)


def _split_ports(text: str) -> list[str]:
    """Split a comma-separated port list, normalising each name."""
    ports = []
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        # Guard against stray words like "Gi1/0/1" vs a wrapped description.
        if chunk and re.match(r"^[A-Za-z][\w\-./:]*$", chunk):
            ports.append(normalise_name(chunk))
    return ports


# ─── VLAN configuration builders ──────────────────────────────────────────────

def build_create_vlan(vlan_id: int, name: str = "") -> list[str]:
    """Config lines to create (or rename) a VLAN."""
    lines = [f"vlan {vlan_id}"]
    if name.strip():
        # IOS VLAN names allow no spaces; the UI validates, this is belt-and-braces.
        lines.append(f"name {name.strip().replace(' ', '_')}")
    lines.append("exit")
    return lines


def build_delete_vlan(vlan_id: int) -> list[str]:
    return [f"no vlan {vlan_id}"]


def build_access_port(interface: str, vlan_id: int, *, voice_vlan: int | None = None,
                      description: str | None = None) -> list[str]:
    """Config lines to put a port into access mode on ``vlan_id``."""
    lines = [
        f"interface {interface}",
        "switchport mode access",
        f"switchport access vlan {vlan_id}",
    ]
    if voice_vlan:
        lines.append(f"switchport voice vlan {voice_vlan}")
    if description is not None:
        lines.append(f"description {description}" if description.strip()
                     else "no description")
    lines.append("exit")
    return lines


def build_trunk_port(interface: str, *, allowed: str = "", native_vlan: int | None = None,
                     encapsulation: str = "dot1q",
                     description: str | None = None) -> list[str]:
    """Config lines to convert a port to a trunk.

    ``encapsulation`` is skipped on switches that only support dot1q — the
    caller retries without it if the device rejects the keyword.
    """
    lines = [f"interface {interface}"]
    if encapsulation:
        lines.append(f"switchport trunk encapsulation {encapsulation}")
    lines.append("switchport mode trunk")
    if allowed.strip():
        lines.append(f"switchport trunk allowed vlan {allowed.strip()}")
    if native_vlan:
        lines.append(f"switchport trunk native vlan {native_vlan}")
    if description is not None:
        lines.append(f"description {description}" if description.strip()
                     else "no description")
    lines.append("exit")
    return lines


def validate_vlan_id(vlan_id: int) -> str:
    """Return an error message for an unusable VLAN id, or '' when valid."""
    if not 1 <= vlan_id <= 4094:
        return "VLAN ID must be between 1 and 4094."
    if 1002 <= vlan_id <= 1005:
        return "VLANs 1002-1005 are reserved by IOS and cannot be created."
    return ""


def validate_vlan_name(name: str) -> str:
    """Return an error message for an unusable VLAN name, or '' when valid."""
    name = name.strip()
    if not name:
        return ""            # optional
    if len(name) > 32:
        return "VLAN names are limited to 32 characters."
    if " " in name:
        return "VLAN names cannot contain spaces — use an underscore instead."
    if not re.match(r"^[\w\-]+$", name):
        return "VLAN names may only contain letters, digits, hyphens and underscores."
    return ""


def parse_vlan_range(text: str) -> tuple[list[int], str]:
    """Parse ``"10,20,30-35"`` into a sorted id list.

    Returns ``(ids, error_message)``; ``error_message`` is non-empty on bad input.
    """
    ids: set[int] = set()
    text = (text or "").strip()
    if not text:
        return [], ""

    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            parts = chunk.split("-")
            if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
                return [], f"'{chunk}' is not a valid VLAN range."
            low, high = int(parts[0]), int(parts[1])
            if low > high:
                return [], f"'{chunk}' has its bounds reversed."
            if high - low > 4094:
                return [], f"'{chunk}' spans too many VLANs."
            ids.update(range(low, high + 1))
        else:
            if not chunk.isdigit():
                return [], f"'{chunk}' is not a valid VLAN ID."
            ids.add(int(chunk))

    invalid = [v for v in ids if not 1 <= v <= 4094]
    if invalid:
        return [], f"VLAN ID {invalid[0]} is out of the 1-4094 range."
    return sorted(ids), ""
