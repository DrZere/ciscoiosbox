"""Transport abstraction.

The rest of the application talks to devices exclusively through
:class:`BaseTransport`. Swapping netmiko for a mock (tests) or a different
driver means implementing this interface — nothing above it changes.

Threading contract: implementations are **not** thread-safe. Exactly one
thread (the :class:`~ciscoiosbox.core.connection.ConnectionWorker`) may touch
a transport instance.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from .models import DeviceInfo, DeviceProfile

#: Called with decoded text as it arrives from the device (for terminal echo).
StreamCallback = Callable[[str], None]


class BaseTransport(ABC):
    """Common interface for SSH / Telnet / Serial device sessions."""

    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile
        self._connected = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> DeviceInfo:
        """Open the session and return gathered device facts.

        Raises the appropriate :mod:`~ciscoiosbox.core.exceptions` subclass on
        failure. Must leave the object disconnected if it raises.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must be safe to call when already closed."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    def is_alive(self) -> bool:
        """Cheap liveness probe used by the keepalive tick."""

    # ── structured command execution ──────────────────────────────────────────

    @abstractmethod
    def send_command(
        self,
        command: str,
        *,
        read_timeout: float | None = None,
        expect_string: str | None = None,
    ) -> str:
        """Run one command and return its full output.

        Implementations must raise
        :class:`~ciscoiosbox.core.exceptions.InvalidInputError` when the device
        reports the command was not understood.
        """

    @abstractmethod
    def send_config(self, commands: list[str]) -> str:
        """Apply configuration lines, entering/leaving config mode as needed."""

    @abstractmethod
    def enable(self) -> bool:
        """Enter privileged EXEC mode. Returns True if we ended up enabled."""

    @abstractmethod
    def find_prompt(self) -> str:
        """Return the device's current prompt string."""

    @abstractmethod
    def save_config(self) -> str:
        """Persist the running configuration to startup."""

    # ── raw / interactive channel (terminal widget) ───────────────────────────

    @abstractmethod
    def write_raw(self, data: str) -> None:
        """Send keystrokes verbatim, with no prompt handling or echo cleanup."""

    @abstractmethod
    def read_raw(self, timeout: float = 0.1) -> str:
        """Non-blocking-ish read of whatever bytes are pending. May return ''."""

    @abstractmethod
    def resize_pty(self, cols: int, rows: int) -> None:
        """Tell the far end the terminal geometry changed. No-op where unsupported."""
