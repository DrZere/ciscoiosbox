"""Detection of IOS-level command rejections.

A Cisco device answers a bad command with a ``%``-prefixed line and a normal
exit status, so netmiko returns it as ordinary output. Every command result
therefore passes through here to be turned into a typed exception.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.exceptions import CommandError, InsufficientPrivilege, InvalidInputError

#: (regex, exception class, user-facing explanation). Order matters — the first
#: match wins, so put the specific patterns before the general ones.
_ERROR_PATTERNS: list[tuple[re.Pattern[str], type[CommandError], str]] = [
    (
        re.compile(r"%\s*Invalid input detected at\s*'?\^'?\s*marker", re.I),
        InvalidInputError,
        "The device did not recognise part of that command.",
    ),
    (
        re.compile(r"%\s*Incomplete command", re.I),
        InvalidInputError,
        "The command was incomplete.",
    ),
    (
        re.compile(r"%\s*Ambiguous command", re.I),
        InvalidInputError,
        "The command was ambiguous — more than one keyword matches.",
    ),
    (
        re.compile(r"%\s*Unknown command|%\s*Bad IP address|%\s*Invalid command", re.I),
        InvalidInputError,
        "The device rejected the command as unknown.",
    ),
    (
        re.compile(r"%\s*Type\s+\"?show\?\"?\s+for a list of subcommands", re.I),
        InvalidInputError,
        "That command is not valid in the current mode.",
    ),
    (
        re.compile(r"%\s*Permission denied|% This command is not authorized", re.I),
        InsufficientPrivilege,
        "This account is not permitted to run that command.",
    ),
    (
        re.compile(r"%\s*VLAN\s+\d+\s+does not exist", re.I),
        CommandError,
        "That VLAN does not exist on the device.",
    ),
    (
        re.compile(r"%\s*Access denied", re.I),
        InsufficientPrivilege,
        "Access was denied.",
    ),
]

#: Rejections that arrive with no ``%`` prefix at all. TACACS+ command
#: authorisation is the important one: a denied command returns this plain
#: sentence, and treating it as ordinary output would silently report a failed
#: configuration change as a success.
_UNPREFIXED_PATTERNS: list[tuple[re.Pattern[str], type[CommandError], str]] = [
    (
        re.compile(r"Command authorization failed", re.I),
        InsufficientPrivilege,
        "Command authorisation was denied for this account by the AAA server.",
    ),
    (
        re.compile(r"Authorization failed", re.I),
        InsufficientPrivilege,
        "Authorisation failed for this command.",
    ),
    (
        re.compile(r"This command is not authorized", re.I),
        InsufficientPrivilege,
        "This account is not permitted to run that command.",
    ),
]

#: Lines starting with these are informational, not failures. IOS emits them
#: routinely, and treating them as errors would break legitimate operations.
_BENIGN = re.compile(
    r"^%\s*(?:"
    r"Warning|Note:|SNMP agent|Default (?:interface|value)|"
    r"Applying config|.*not configured on interface|"
    r"Portfast has been configured|"
    r"Please save|Building configuration|"
    r"The current configuration was|"
    r"Access VLAN does not exist"
    r")",
    re.I,
)


@dataclass
class IosError:
    """A rejection found in command output."""

    matched_line: str
    explanation: str
    exception_class: type[CommandError]

    def as_exception(self, *, command: str = "", output: str = "") -> CommandError:
        """Build the exception to raise, with the device's own line included."""
        message = f"{self.explanation}\n\nDevice replied: {self.matched_line.strip()}"
        return self.exception_class(message, command=command, output=output or self.matched_line)


def find_ios_error(output: str) -> IosError | None:
    """Return the first genuine rejection in ``output``, or None if it is clean."""
    if not output:
        return None

    # Unprefixed rejections first — they carry no "%" so the loop below, which
    # only considers "%" lines, would never see them.
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, exc_class, explanation in _UNPREFIXED_PATTERNS:
            if pattern.search(stripped):
                return IosError(stripped, explanation, exc_class)

    if "%" not in output:
        return None

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("%"):
            continue
        if _BENIGN.match(stripped):
            continue
        for pattern, exc_class, explanation in _ERROR_PATTERNS:
            if pattern.search(stripped):
                return IosError(stripped, explanation, exc_class)

    # A "^" caret marker on its own line is IOS pointing at the bad token; the
    # accompanying % line is usually the next one, but catch the case where the
    # wording is unfamiliar.
    if re.search(r"^\s*\^\s*$", output, re.M):
        for line in output.splitlines():
            if line.strip().startswith("%") and not _BENIGN.match(line.strip()):
                return IosError(
                    line.strip(),
                    "The device rejected the command.",
                    InvalidInputError,
                )
    return None


def raise_for_ios_error(command: str, output: str) -> None:
    """Raise the matching typed exception if ``output`` contains a rejection."""
    error = find_ios_error(output)
    if error is not None:
        raise error.as_exception(command=command, output=output)


def looks_like_paging(output: str) -> bool:
    """True when output was truncated by a ``--More--`` prompt."""
    return "--More--" in output or "--more--" in output
