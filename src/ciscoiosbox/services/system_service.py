"""System configuration: hostname, management addressing, saving config."""
from __future__ import annotations

import logging

from PySide6.QtCore import Signal

from ..core.exceptions import CiscoIOSBoxError
from ..core.models import DeviceInfo
from ..core.transport import BaseTransport
from ..parsers import system as parse_sys
from .base import BaseService

log = logging.getLogger(__name__)


class SystemService(BaseService):
    """Basic device administration."""

    kinds = ("sys_facts", "sys_hostname", "sys_mgmt", "sys_save",
             "sys_config_fetch", "sys_command")

    facts_loaded = Signal(object)              # DeviceInfo
    #: (hostname, mgmt_interface, ip, mask, gateway) read back from the config.
    mgmt_loaded = Signal(str, str, str, str, str)
    running_config_loaded = Signal(str)
    command_output = Signal(str, str)          # (command, output)
    action_completed = Signal(str)
    hostname_changed = Signal(str)
    #: True when the running config differs from startup (unsaved changes).
    unsaved_changes = Signal(bool)

    # ── reads ─────────────────────────────────────────────────────────────────

    def refresh_facts(self) -> str:
        platform = self.controller.profile.device_type_base

        def work(transport: BaseTransport) -> DeviceInfo:
            return parse_sys.parse_show_version(
                transport.send_command("show version", read_timeout=30), platform)

        return self._submit(work, "sys_facts", self.facts_loaded.emit,
                            label="show version")

    def load_management_config(self, interface: str = "") -> str:
        """Read hostname, gateway and the management interface's address.

        ``interface`` may be blank, in which case we look for the first
        SVI/management port carrying an address.
        """
        def work(transport: BaseTransport) -> tuple[str, str, str, str, str]:
            hostname = parse_sys.parse_hostname(
                transport.send_command("show running-config | include ^hostname"))

            gateway = parse_sys.parse_default_gateway(transport.send_command(
                "show running-config | include ^ip (default-gateway|route 0.0.0.0)"))

            target = interface
            if not target:
                # Pick the first interface that actually has an address; that is
                # almost always the management SVI.
                from ..parsers.interfaces import parse_ip_interface_brief

                rows = parse_ip_interface_brief(
                    transport.send_command("show ip interface brief"))
                candidates = [r for r in rows if r.ip_address]
                preferred = next(
                    (r for r in candidates if r.name.startswith(("Vlan", "GigabitEthernet0/0",
                                                                 "FastEthernet0/0",
                                                                 "Management"))),
                    None)
                chosen = preferred or (candidates[0] if candidates else None)
                target = chosen.name if chosen else ""

            ip = mask = ""
            if target:
                fragment = transport.send_command(
                    f"show running-config interface {target}")
                ip, mask = parse_sys.parse_interface_address(fragment)

            return hostname, target, ip, mask, gateway

        return self._submit(
            work, "sys_mgmt",
            lambda r: self.mgmt_loaded.emit(*r),
            label="load management config")

    def load_running_config(self) -> str:
        """Fetch the full running configuration for viewing or export."""
        def work(transport: BaseTransport) -> str:
            # Large configs on a slow console need generous headroom.
            return transport.send_command("show running-config", read_timeout=120)

        return self._submit(work, "sys_config_fetch", self.running_config_loaded.emit,
                            label="show running-config")

    def check_unsaved_changes(self) -> str:
        """Compare running and startup config sizes to detect pending changes.

        A byte-count comparison is cheap and catches the common case; it is a
        hint for the UI badge, not a guarantee.
        """
        def work(transport: BaseTransport) -> bool:
            import re

            output = transport.send_command("show running-config | include ^!$")
            running_marker = len(output)
            try:
                startup = transport.send_command("show startup-config | include ^!$")
            except CiscoIOSBoxError:
                return False
            if "startup-config is not present" in startup.lower():
                return True
            # Also compare the "Current configuration : N bytes" headers when present.
            def size_of(text: str) -> int:
                match = re.search(r"Current configuration\s*:\s*(\d+) bytes", text)
                return int(match.group(1)) if match else len(text)

            return size_of(startup) != running_marker and abs(
                len(startup) - running_marker) > 2

        return self._submit(work, "sys_command", self.unsaved_changes.emit,
                            label="check unsaved changes")

    def run_command(self, command: str) -> str:
        """Run an arbitrary read-only command and hand back its raw output."""
        def work(transport: BaseTransport) -> tuple[str, str]:
            return command, transport.send_command(command, read_timeout=60)

        return self._submit(
            work, "sys_command",
            lambda r: self.command_output.emit(r[0], r[1]),
            label=command)

    # ── writes ────────────────────────────────────────────────────────────────

    def set_hostname(self, hostname: str) -> str:
        """Change the device hostname.

        The prompt changes as this command applies, which normally confuses
        netmiko's prompt tracking — so we re-baseline the prompt afterwards.
        """
        problem = parse_sys.validate_hostname(hostname)
        if problem:
            self.error.emit(problem, "sys_hostname")
            return ""

        def work(transport: BaseTransport) -> str:
            transport.send_config([f"hostname {hostname.strip()}"])
            # Re-read the prompt so later commands still match correctly.
            transport.find_prompt()
            return hostname.strip()

        def on_success(name: str) -> None:
            self.hostname_changed.emit(name)
            self.action_completed.emit(f"Hostname changed to {name}.")
            self.unsaved_changes.emit(True)

        return self._submit(work, "sys_hostname", on_success,
                            label=f"hostname {hostname}")

    def set_management_ip(self, interface: str, ip: str, mask: str,
                          gateway: str = "", *, use_dhcp: bool = False,
                          no_shutdown: bool = True) -> str:
        """Configure the management address and default gateway.

        Guard rail: reconfiguring the interface we are *currently connected
        through* will drop the session. The caller is expected to have warned the
        user; this method still applies the change, because sometimes that is
        exactly the intent (e.g. over console).
        """
        if not use_dhcp:
            for value, validator in ((ip, parse_sys.validate_ipv4),
                                     (mask, parse_sys.validate_netmask)):
                problem = validator(value)
                if problem:
                    self.error.emit(problem, "sys_mgmt")
                    return ""
            if gateway:
                problem = parse_sys.validate_ipv4(gateway, allow_empty=True)
                if problem:
                    self.error.emit(problem, "sys_mgmt")
                    return ""

        def work(transport: BaseTransport) -> str:
            commands = [f"interface {interface}"]
            commands.append("ip address dhcp" if use_dhcp
                            else f"ip address {ip} {mask}")
            if no_shutdown:
                commands.append("no shutdown")
            commands.append("exit")
            transport.send_config(commands)

            if gateway:
                # Switches use `ip default-gateway`; routers need a static route.
                # Try the switch form first and fall back automatically.
                try:
                    transport.send_config([f"ip default-gateway {gateway}"])
                except CiscoIOSBoxError:
                    transport.send_config([f"ip route 0.0.0.0 0.0.0.0 {gateway}"])
            return interface

        def on_success(name: str) -> None:
            target = "DHCP" if use_dhcp else f"{ip} {mask}"
            self.action_completed.emit(f"Set {name} to {target}.")
            self.unsaved_changes.emit(True)

        return self._submit(work, "sys_mgmt", on_success,
                            label=f"mgmt ip {interface}")

    def save_config(self) -> str:
        """Copy running-config to startup-config."""
        def work(transport: BaseTransport) -> str:
            return transport.save_config()

        def on_success(output: str) -> None:
            self.action_completed.emit("Configuration saved to startup-config.")
            self.unsaved_changes.emit(False)
            log.debug("save_config output: %s", output)

        return self._submit(work, "sys_save", on_success, label="write memory")

    def reload_device(self, save_first: bool = True) -> str:
        """Reboot the device.

        Deliberately not wired to a toolbar button — the caller must confirm.
        The session will drop as a result, which the connection worker reports
        as a lost session.
        """
        def work(transport: BaseTransport) -> str:
            if save_first:
                transport.save_config()
            # `reload` prompts for confirmation; answer it and expect no prompt back.
            transport.write_raw("reload\n")
            import time
            time.sleep(1.0)
            pending = transport.read_raw()
            if "confirm" in pending.lower() or "[yes/no]" in pending.lower():
                transport.write_raw("y\n")
            elif "[confirm]" in pending.lower():
                transport.write_raw("\n")
            return "reload issued"

        return self._submit(
            work, "sys_command",
            lambda _: self.action_completed.emit("Reload command sent to the device."),
            label="reload")
