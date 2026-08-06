"""System-level parsers: version facts, CPU and memory utilisation."""
from __future__ import annotations

import re
import time

from ..core.models import DeviceInfo, ResourceSample
from . import textfsm_parser as tfsm


# ─── show version ─────────────────────────────────────────────────────────────

def parse_show_version(output: str, platform: str = "cisco_ios") -> DeviceInfo:
    """Extract device facts from ``show version``."""
    rows = tfsm.parse("show version", output, platform)
    if rows:
        row = rows[0]
        return DeviceInfo(
            hostname=tfsm.get(row, "hostname", "host_name"),
            model=tfsm.get(row, "hardware", "model", "platform") or _first_list(row, "hardware"),
            version=tfsm.get(row, "version", "os_version"),
            serial_number=tfsm.get(row, "serial", "serial_number") or _first_list(row, "serial"),
            uptime=tfsm.get(row, "uptime"),
            image=tfsm.get(row, "running_image", "image", "software_image"),
        )
    return _parse_version_regex(output)


def _first_list(row: dict, key: str) -> str:
    """ntc-templates returns hardware/serial as lists on stacked switches."""
    value = row.get(key)
    if isinstance(value, list) and value:
        return str(value[0])
    return ""


def _parse_version_regex(output: str) -> DeviceInfo:
    info = DeviceInfo()

    match = re.search(r"^(\S+)\s+uptime is\s+(.+)$", output, re.M)
    if match:
        info.hostname = match.group(1).strip()
        info.uptime = match.group(2).strip()

    # "Cisco IOS Software, ... Version 15.2(4)E7, RELEASE SOFTWARE"
    # "Cisco IOS XE Software, Version 16.09.04"
    match = re.search(r"Version\s+([\w.()\-]+)", output)
    if match:
        info.version = match.group(1).rstrip(",")

    # "cisco WS-C2960X-24TS-L (PowerPC405) processor"
    match = re.search(r"^cisco\s+(\S+)\s+\(", output, re.M | re.I)
    if match:
        info.model = match.group(1)
    else:
        match = re.search(r"^Model [Nn]umber\s*:\s*(\S+)", output, re.M)
        if match:
            info.model = match.group(1)

    match = re.search(r"^(?:System s|S)erial [Nn]umber\s*:\s*(\S+)", output, re.M)
    if match:
        info.serial_number = match.group(1)

    match = re.search(r'System image file is\s+"([^"]+)"', output)
    if match:
        info.image = match.group(1)

    return info


# ─── CPU utilisation ──────────────────────────────────────────────────────────

#: IOS: "CPU utilization for five seconds: 7%/0%; one minute: 8%; five minutes: 9%"
_CPU_IOS = re.compile(
    r"CPU utilization for five seconds:\s*(\d+)%(?:/(\d+)%)?;\s*"
    r"one minute:\s*(\d+)%;\s*five minutes:\s*(\d+)%",
    re.I,
)

#: NX-OS: "CPU util  :   3.5% user,   1.0% kernel,  95.5% idle"
_CPU_NXOS = re.compile(
    r"CPU util\S*\s*:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*kernel", re.I)

#: IOS-XR / some XE: "CPU utilization for one minute: 12%"
_CPU_SIMPLE = re.compile(r"CPU utilization for (?:one|1) minute:\s*(\d+)%", re.I)


def parse_cpu(output: str) -> tuple[float, float, float]:
    """Return ``(cpu_5sec, cpu_1min, cpu_5min)`` as percentages.

    Falls back through several platform wordings; returns zeros when none match
    so a monitoring poll degrades to a flat line rather than an exception.
    """
    match = _CPU_IOS.search(output)
    if match:
        return (float(match.group(1)), float(match.group(3)), float(match.group(4)))

    match = _CPU_NXOS.search(output)
    if match:
        total = float(match.group(1)) + float(match.group(2))
        return (total, total, total)

    match = _CPU_SIMPLE.search(output)
    if match:
        value = float(match.group(1))
        return (value, value, value)

    return (0.0, 0.0, 0.0)


# ─── Memory utilisation ───────────────────────────────────────────────────────

#: `show processes memory`:
#: "Processor Pool Total: 212933812 Used:  74253464 Free: 138680348"
_MEM_PROCESSES = re.compile(
    r"Processor Pool Total:\s*(\d+)\s*Used:\s*(\d+)\s*Free:\s*(\d+)", re.I)

#: `show memory statistics` table row:
#: "Processor   2A3B4C5D   212933812    74253464   138680348   ..."
_MEM_STATISTICS = re.compile(
    r"^\s*Processor\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)", re.I | re.M)

#: NX-OS `show system resources`:
#: "Memory usage:   8151656K total,   3129404K used,   5022252K free"
_MEM_NXOS = re.compile(
    r"Memory usage:\s*(\d+)K total,\s*(\d+)K used,\s*(\d+)K free", re.I)

#: IOS-XE `show platform resources` / `show memory summary` variants
_MEM_SUMMARY = re.compile(
    r"^\s*Processor\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s*$", re.I | re.M)


def parse_memory(output: str) -> tuple[int, int]:
    """Return ``(used_bytes, free_bytes)``. Zeros when nothing matched."""
    match = _MEM_PROCESSES.search(output)
    if match:
        return (int(match.group(2)), int(match.group(3)))

    match = _MEM_STATISTICS.search(output)
    if match:
        # Columns are Total, Used, Free.
        return (int(match.group(2)), int(match.group(3)))

    match = _MEM_NXOS.search(output)
    if match:
        return (int(match.group(2)) * 1024, int(match.group(3)) * 1024)

    match = _MEM_SUMMARY.search(output)
    if match:
        return (int(match.group(2)), int(match.group(3)))

    return (0, 0)


def build_resource_sample(cpu_output: str, mem_output: str,
                          timestamp: float | None = None) -> ResourceSample:
    """Combine CPU and memory command output into one graph sample."""
    cpu_5s, cpu_1m, cpu_5m = parse_cpu(cpu_output)
    used, free = parse_memory(mem_output)
    return ResourceSample(
        timestamp=timestamp if timestamp is not None else time.time(),
        cpu_5sec=cpu_5s, cpu_1min=cpu_1m, cpu_5min=cpu_5m,
        mem_used_bytes=used, mem_free_bytes=free,
    )


# ─── Running config helpers ───────────────────────────────────────────────────

def parse_hostname(output: str) -> str:
    """Pull the hostname out of a running-config fragment."""
    match = re.search(r"^hostname\s+(\S+)", output, re.M)
    return match.group(1) if match else ""


def parse_default_gateway(output: str) -> str:
    """Find the default route or ``ip default-gateway`` in a config fragment."""
    match = re.search(r"^ip default-gateway\s+(\d{1,3}(?:\.\d{1,3}){3})", output, re.M)
    if match:
        return match.group(1)
    match = re.search(
        r"^ip route\s+0\.0\.0\.0\s+0\.0\.0\.0\s+(\d{1,3}(?:\.\d{1,3}){3})", output, re.M)
    return match.group(1) if match else ""


def parse_interface_address(output: str) -> tuple[str, str]:
    """Return ``(ip, mask)`` from an interface config fragment."""
    match = re.search(
        r"^\s*ip address\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})",
        output, re.M)
    if match:
        return match.group(1), match.group(2)
    if re.search(r"^\s*ip address dhcp", output, re.M):
        return "dhcp", ""
    return "", ""


# ─── Validation used by the system-config form ────────────────────────────────

_HOSTNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{0,62}$")


def validate_hostname(name: str) -> str:
    """Return an error message for an invalid IOS hostname, or ''."""
    name = name.strip()
    if not name:
        return "Hostname cannot be empty."
    if len(name) > 63:
        return "Hostnames are limited to 63 characters."
    if not _HOSTNAME_RE.match(name):
        return ("Hostnames must start with a letter and contain only letters, "
                "digits and hyphens.")
    if name.endswith("-"):
        return "Hostnames cannot end with a hyphen."
    return ""


def validate_ipv4(address: str, *, allow_empty: bool = False) -> str:
    """Return an error message for an invalid dotted-quad address, or ''."""
    address = address.strip()
    if not address:
        return "" if allow_empty else "Enter an IPv4 address."
    parts = address.split(".")
    if len(parts) != 4:
        return f"'{address}' is not a valid IPv4 address."
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255 or (
                len(part) > 1 and part.startswith("0")):
            return f"'{address}' is not a valid IPv4 address."
    return ""


#: The 33 legal contiguous subnet masks, /0 through /32.
_VALID_MASKS = {
    ".".join(str((0xFFFFFFFF << (32 - bits) >> shift) & 0xFF)
             for shift in (24, 16, 8, 0)): bits
    for bits in range(33)
}


def validate_netmask(mask: str) -> str:
    """Return an error message for a non-contiguous or malformed mask, or ''."""
    basic = validate_ipv4(mask)
    if basic:
        return "Enter a valid subnet mask." if mask.strip() else "Enter a subnet mask."
    if mask.strip() not in _VALID_MASKS:
        return f"'{mask.strip()}' is not a valid subnet mask (bits must be contiguous)."
    return ""


def mask_to_prefix(mask: str) -> int:
    """Convert ``255.255.255.0`` → ``24``. Returns -1 for an invalid mask."""
    return _VALID_MASKS.get(mask.strip(), -1)


def prefix_to_mask(prefix: int) -> str:
    """Convert ``24`` → ``255.255.255.0``."""
    if not 0 <= prefix <= 32:
        return ""
    return ".".join(
        str((0xFFFFFFFF << (32 - prefix) >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def same_subnet(ip_a: str, ip_b: str, mask: str) -> bool:
    """True when two addresses share a subnet — used to sanity-check a gateway."""
    if any(validate_ipv4(v) for v in (ip_a, ip_b)) or validate_netmask(mask):
        return False

    def to_int(value: str) -> int:
        octets = [int(o) for o in value.split(".")]
        return (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]

    mask_int = to_int(mask)
    return (to_int(ip_a) & mask_int) == (to_int(ip_b) & mask_int)
