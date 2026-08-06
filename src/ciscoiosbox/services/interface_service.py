"""Interface listing and administrative control."""
from __future__ import annotations

import logging

from PySide6.QtCore import Signal

from ..core.exceptions import CiscoIOSBoxError, InvalidInputError
from ..core.models import InterfaceRow
from ..core.transport import BaseTransport
from ..parsers import interfaces as parse_intf
from .base import BaseService

log = logging.getLogger(__name__)


class InterfaceService(BaseService):
    """Fetches the interface grid and applies per-port changes."""

    kinds = ("intf_refresh", "intf_admin", "intf_describe", "intf_detail")

    #: The full, merged interface list.
    interfaces_loaded = Signal(list)          # list[InterfaceRow]
    #: (interface_name, now_shutdown) after a successful admin-state change.
    admin_state_changed = Signal(str, bool)
    #: (interface_name, raw_output) from `show interfaces <name>`.
    detail_loaded = Signal(str, str)
    #: Human-readable confirmation for the status bar.
    action_completed = Signal(str)

    # ── refresh ───────────────────────────────────────────────────────────────

    def refresh(self) -> str:
        """Reload the interface grid.

        Runs up to four commands in a single task so the grid updates atomically
        rather than flickering through partial states.
        """
        platform = self.controller.profile.device_type_base

        def work(transport: BaseTransport) -> list[InterfaceRow]:
            # `show ip interface brief` is the only universally available source
            # for admin state, so a failure here is fatal to the refresh.
            ip_brief = parse_intf.parse_ip_interface_brief(
                transport.send_command("show ip interface brief"), platform)

            # The rest are switch-only or platform-dependent enrichments. Each is
            # optional: a router has no `show interfaces status`, and we must not
            # turn that into a visible error.
            status: list[InterfaceRow] = []
            try:
                status = parse_intf.parse_interfaces_status(
                    transport.send_command("show interfaces status"), platform)
            except InvalidInputError:
                log.debug("Device has no 'show interfaces status' (likely a router)")
            except CiscoIOSBoxError as exc:
                log.debug("show interfaces status failed: %s", exc)

            descriptions: dict[str, str] = {}
            if not status:
                # Routers expose descriptions here instead.
                try:
                    descriptions = parse_intf.parse_interface_descriptions(
                        transport.send_command("show interfaces description"))
                except CiscoIOSBoxError as exc:
                    log.debug("show interfaces description failed: %s", exc)

            modes: dict[str, str] = {}
            if status:
                try:
                    modes = parse_intf.parse_switchport_modes(
                        transport.send_command("show interfaces switchport",
                                               read_timeout=45))
                except CiscoIOSBoxError as exc:
                    log.debug("show interfaces switchport failed: %s", exc)

            return parse_intf.merge_interface_data(ip_brief, status, descriptions, modes)

        return self._submit(work, "intf_refresh", self.interfaces_loaded.emit,
                            label="refresh interfaces")

    def load_detail(self, interface: str) -> str:
        """Fetch raw ``show interfaces <name>`` output for the detail pane."""
        def work(transport: BaseTransport) -> tuple[str, str]:
            return interface, transport.send_command(f"show interfaces {interface}")

        return self._submit(
            work, "intf_detail",
            lambda result: self.detail_loaded.emit(result[0], result[1]),
            label=f"detail {interface}")

    # ── mutations ─────────────────────────────────────────────────────────────

    def set_admin_state(self, interface: str, shutdown: bool) -> str:
        """Shut down or bring up a port.

        After applying the change we re-read ``show ip interface brief`` for just
        this port and report the state the device actually landed in, rather than
        assuming the config took effect.
        """
        command = "shutdown" if shutdown else "no shutdown"

        def work(transport: BaseTransport) -> tuple[str, bool]:
            transport.send_config([f"interface {interface}", command, "exit"])
            # Verify instead of trusting: an interface can stay down for reasons
            # unrelated to the admin state we just set.
            verify = transport.send_command(
                f"show ip interface brief {interface}")
            rows = parse_intf.parse_ip_interface_brief(verify)
            actually_shutdown = rows[0].is_shutdown if rows else shutdown
            return interface, actually_shutdown

        def on_success(result: tuple[str, bool]) -> None:
            name, is_shut = result
            self.admin_state_changed.emit(name, is_shut)
            self.action_completed.emit(
                f"{name} is now {'shut down' if is_shut else 'enabled'}.")
            if is_shut != shutdown:
                self.error.emit(
                    f"{name} did not reach the requested state — the device "
                    f"reports it as {'shut down' if is_shut else 'up'}.",
                    "intf_admin")

        return self._submit(work, "intf_admin", on_success,
                            label=f"{command} {interface}")

    def set_description(self, interface: str, description: str) -> str:
        """Set or clear a port description."""
        def work(transport: BaseTransport) -> str:
            line = f"description {description.strip()}" if description.strip() \
                else "no description"
            transport.send_config([f"interface {interface}", line, "exit"])
            return interface

        return self._submit(
            work, "intf_describe",
            lambda name: self.action_completed.emit(f"Updated the description on {name}."),
            label=f"describe {interface}")

    def set_speed_duplex(self, interface: str, speed: str, duplex: str) -> str:
        """Force speed/duplex, or return either to ``auto``."""
        def work(transport: BaseTransport) -> str:
            commands = [f"interface {interface}"]
            commands.append("speed auto" if speed == "auto" else f"speed {speed}")
            commands.append("duplex auto" if duplex == "auto" else f"duplex {duplex}")
            commands.append("exit")
            transport.send_config(commands)
            return interface

        return self._submit(
            work, "intf_describe",
            lambda name: self.action_completed.emit(
                f"Set {name} to {speed}/{duplex}."),
            label=f"speed/duplex {interface}")
