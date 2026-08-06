"""Typed exception hierarchy for CiscoIOSBox.

Every failure the UI needs to distinguish gets its own class, so views can
branch on type instead of pattern-matching error strings. Each carries a
``user_message`` suitable for direct display in a toast or dialog.
"""
from __future__ import annotations


class CiscoIOSBoxError(Exception):
    """Base class for every error raised by this application."""

    #: Fallback shown when a subclass does not override it.
    default_message = "An unexpected error occurred."

    def __init__(self, message: str = "", *, detail: str = "") -> None:
        self.user_message = message or self.default_message
        self.detail = detail
        super().__init__(self.user_message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user_message} ({self.detail})" if self.detail else self.user_message


# ─── Connection lifecycle ─────────────────────────────────────────────────────

class ConnectionError_(CiscoIOSBoxError):
    """Base for anything that goes wrong establishing or holding a session."""

    default_message = "Could not connect to the device."


class AuthenticationError(ConnectionError_):
    default_message = (
        "Authentication failed. Check the username and password, and confirm "
        "the account is permitted to log in."
    )


class ConnectionTimeout(ConnectionError_):
    default_message = (
        "The device did not respond in time. Verify the host is reachable and "
        "that the port is open."
    )


class ConnectionRefused(ConnectionError_):
    default_message = (
        "The device refused the connection. Confirm the service is enabled and "
        "listening on the configured port."
    )


class SerialPortError(ConnectionError_):
    default_message = (
        "Could not open the serial port. It may be in use by another program, "
        "or you may lack permission to access it."
    )


class SessionLost(ConnectionError_):
    default_message = "The connection to the device was lost."


class NotConnected(ConnectionError_):
    default_message = "Not connected to a device."


# ─── Command execution ────────────────────────────────────────────────────────

class CommandError(CiscoIOSBoxError):
    """The device accepted the bytes but rejected the command."""

    default_message = "The device rejected the command."

    def __init__(self, message: str = "", *, command: str = "", output: str = "") -> None:
        self.command = command
        self.output = output
        super().__init__(message, detail=command)


class InvalidInputError(CommandError):
    default_message = "The device reported invalid input for that command."


class InsufficientPrivilege(CommandError):
    default_message = (
        "This action requires privileged EXEC mode. Set an enable password on "
        "the session profile and reconnect."
    )


class EnableFailed(ConnectionError_):
    default_message = (
        "Could not enter privileged EXEC mode. The enable password may be wrong."
    )


class ParseError(CiscoIOSBoxError):
    default_message = "Could not interpret the device's response."


# ─── Credentials / storage ────────────────────────────────────────────────────

class CredentialError(CiscoIOSBoxError):
    default_message = "Could not access stored credentials."


class VaultLocked(CredentialError):
    default_message = "The credential vault is locked. Enter the master password to unlock."


# ─── SNMP ─────────────────────────────────────────────────────────────────────

class SnmpError(CiscoIOSBoxError):
    default_message = "The SNMP request failed."


class SnmpTimeout(SnmpError):
    default_message = (
        "The device did not answer the SNMP query. Check the community string "
        "or v3 credentials and confirm SNMP is enabled."
    )
