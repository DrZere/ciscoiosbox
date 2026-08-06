"""VLAN listing, creation, deletion and port assignment."""
from __future__ import annotations

import logging

from PySide6.QtCore import Signal

from ..core.exceptions import CiscoIOSBoxError, InvalidInputError
from ..core.models import Vlan
from ..core.transport import BaseTransport
from ..parsers import vlans as parse_vlan
from .base import BaseService

log = logging.getLogger(__name__)


class VlanService(BaseService):
    """Device-side VLAN operations."""

    kinds = ("vlan_refresh", "vlan_create", "vlan_delete", "vlan_assign")

    vlans_loaded = Signal(list)               # list[Vlan]
    action_completed = Signal(str)
    #: Emitted after any change so the view can refresh itself.
    changed = Signal()

    # ── read ──────────────────────────────────────────────────────────────────

    def refresh(self) -> str:
        platform = self.controller.profile.device_type_base

        def work(transport: BaseTransport) -> list[Vlan]:
            return parse_vlan.parse_show_vlan_brief(
                transport.send_command("show vlan brief"), platform)

        def on_error(exc: CiscoIOSBoxError) -> None:
            # A router in L3-only mode has no VLAN database. Report an empty list
            # with a clear note rather than an error dialog.
            if isinstance(exc, InvalidInputError):
                self.vlans_loaded.emit([])
                self.error.emit(
                    "This device does not support VLANs (no VLAN database found).",
                    "vlan_refresh")
            else:
                self.error.emit(exc.user_message, "vlan_refresh")

        return self._submit(work, "vlan_refresh", self.vlans_loaded.emit, on_error,
                            label="refresh vlans")

    # ── write ─────────────────────────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str = "") -> str:
        """Create one VLAN, then confirm it appears in the database."""
        def work(transport: BaseTransport) -> Vlan:
            transport.send_config(parse_vlan.build_create_vlan(vlan_id, name))
            found = parse_vlan.parse_show_vlan_brief(
                transport.send_command(f"show vlan id {vlan_id}"))
            if not found:
                raise CiscoIOSBoxError(
                    f"VLAN {vlan_id} was not present after the change was applied.")
            return found[0]

        def on_success(vlan: Vlan) -> None:
            self.action_completed.emit(
                f"Created VLAN {vlan.vlan_id}"
                + (f" ({vlan.name})." if vlan.name else "."))
            self.changed.emit()

        return self._submit(work, "vlan_create", on_success,
                            label=f"create vlan {vlan_id}")

    def create_vlans(self, vlan_ids: list[int], name_prefix: str = "") -> str:
        """Create several VLANs in one config session.

        Reports partial success: a rejected id does not abort the others, which
        matters when a range overlaps something that already exists.
        """
        def work(transport: BaseTransport) -> tuple[list[int], list[tuple[int, str]]]:
            created: list[int] = []
            failed: list[tuple[int, str]] = []
            for vlan_id in vlan_ids:
                name = f"{name_prefix}{vlan_id}" if name_prefix else ""
                try:
                    transport.send_config(parse_vlan.build_create_vlan(vlan_id, name))
                    created.append(vlan_id)
                except CiscoIOSBoxError as exc:
                    failed.append((vlan_id, exc.user_message))
            return created, failed

        def on_success(result: tuple[list[int], list[tuple[int, str]]]) -> None:
            created, failed = result
            if created:
                self.action_completed.emit(
                    f"Created {len(created)} VLAN(s): "
                    + ", ".join(str(v) for v in created[:10])
                    + ("…" if len(created) > 10 else ""))
            self.changed.emit()
            if failed:
                detail = "; ".join(f"VLAN {v}: {m}" for v, m in failed[:5])
                self.error.emit(
                    f"{len(failed)} VLAN(s) could not be created. {detail}",
                    "vlan_create")

        return self._submit(work, "vlan_create", on_success,
                            label=f"create {len(vlan_ids)} vlans")

    def delete_vlan(self, vlan_id: int) -> str:
        """Delete a VLAN. Ports in it are left orphaned, as IOS does."""
        def work(transport: BaseTransport) -> int:
            transport.send_config(parse_vlan.build_delete_vlan(vlan_id))
            return vlan_id

        def on_success(deleted_id: int) -> None:
            self.action_completed.emit(f"Deleted VLAN {deleted_id}.")
            self.changed.emit()

        return self._submit(work, "vlan_delete", on_success,
                            label=f"delete vlan {vlan_id}")

    def assign_access_port(self, interfaces: list[str], vlan_id: int,
                           voice_vlan: int | None = None) -> str:
        """Put one or more ports into access mode on ``vlan_id``."""
        def work(transport: BaseTransport) -> tuple[int, list[str], list[tuple[str, str]]]:
            done: list[str] = []
            failed: list[tuple[str, str]] = []
            for interface in interfaces:
                try:
                    transport.send_config(parse_vlan.build_access_port(
                        interface, vlan_id, voice_vlan=voice_vlan))
                    done.append(interface)
                except CiscoIOSBoxError as exc:
                    failed.append((interface, exc.user_message))
            return vlan_id, done, failed

        return self._submit(work, "vlan_assign",
                            lambda r: self._report_assign(r, "access"),
                            label=f"access vlan {vlan_id}")

    def assign_trunk_port(self, interfaces: list[str], allowed: str = "",
                          native_vlan: int | None = None) -> str:
        """Convert one or more ports to 802.1Q trunks."""
        def work(transport: BaseTransport) -> tuple[int, list[str], list[tuple[str, str]]]:
            done: list[str] = []
            failed: list[tuple[str, str]] = []
            for interface in interfaces:
                try:
                    transport.send_config(parse_vlan.build_trunk_port(
                        interface, allowed=allowed, native_vlan=native_vlan))
                    done.append(interface)
                except InvalidInputError:
                    # Switches with only dot1q reject the encapsulation command;
                    # retry without it before giving up on the port.
                    try:
                        transport.send_config(parse_vlan.build_trunk_port(
                            interface, allowed=allowed, native_vlan=native_vlan,
                            encapsulation=""))
                        done.append(interface)
                    except CiscoIOSBoxError as exc:
                        failed.append((interface, exc.user_message))
                except CiscoIOSBoxError as exc:
                    failed.append((interface, exc.user_message))
            return 0, done, failed

        return self._submit(work, "vlan_assign",
                            lambda r: self._report_assign(r, "trunk"),
                            label="trunk ports")

    def _report_assign(self, result: tuple[int, list[str], list[tuple[str, str]]],
                       mode: str) -> None:
        """Shared success/partial-failure reporting for port assignment."""
        vlan_id, done, failed = result
        if done:
            target = f" on VLAN {vlan_id}" if mode == "access" and vlan_id else ""
            self.action_completed.emit(
                f"Set {len(done)} port(s) to {mode} mode{target}: "
                + ", ".join(done[:6]) + ("…" if len(done) > 6 else ""))
        self.changed.emit()
        if failed:
            detail = "; ".join(f"{i}: {m}" for i, m in failed[:4])
            self.error.emit(f"{len(failed)} port(s) failed. {detail}", "vlan_assign")
