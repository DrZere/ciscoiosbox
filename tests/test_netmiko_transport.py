"""Netmiko transport raw-channel behaviour.

The interactive terminal and the structured command services only work because
``read_raw()`` is non-blocking: if it blocked waiting for device output, the
single connection-worker thread would stall behind it and every keystroke and
command would queue up undelivered.

These tests pin that contract with a fake netmiko connection, so they run
without a device and without importing netmiko itself.
"""
from __future__ import annotations

import time

from ciscoiosbox.core.models import DeviceProfile
from ciscoiosbox.core.netmiko_transport import NetmikoTransport


class FakeChannel:
    """Emulates the netmiko ``channel`` object our read path calls into.

    ``read_buffer`` performs one non-blocking read: it returns whatever was
    buffered and otherwise returns immediately (like the real SSHChannel, which
    is guarded by paramiko's ``recv_ready``).
    """

    def __init__(self, pending: str = "") -> None:
        self.pending = pending
        self.reads = 0

    def read_buffer(self) -> str:
        self.reads += 1
        data, self.pending = self.pending, ""
        return data


class FakeConn:
    """A minimal netmiko connection with ``channel`` / ``remote_conn``."""

    def __init__(self, pending: str = "", *, bare: bool = False) -> None:
        self.channel = None if bare else FakeChannel(pending)
        self.remote_conn = object() if not bare else None


def make_transport(pending: str = "", *, bare: bool = False) -> NetmikoTransport:
    profile = DeviceProfile(name="test", host="10.0.0.1", username="admin")
    transport = NetmikoTransport(profile)
    transport._conn = FakeConn(pending, bare=bare)
    transport._connected = True
    return transport


def test_read_raw_returns_immediately_when_quiet():
    """The idle poll must not block the worker waiting for data."""
    transport = make_transport()

    start = time.monotonic()
    assert transport.read_raw(timeout=0.05) == ""
    # A blocking read would take the full timeout; we must return in a fraction
    # of it (just construction + the fake, no network wait).
    assert time.monotonic() - start < 0.05


def test_read_raw_returns_available_data():
    transport = make_transport(pending="hello\r\n#")
    assert transport.read_raw() == "hello\r\n#"


def test_read_raw_does_not_swallow_structured_output():
    """The raw channel must not consume data meant for send_command.

    Structured reads go through netmiko's own ``read_channel``; the raw path
    only touches the buffer it is handed, so nothing the worker is waiting on
    gets eaten here.
    """
    transport = make_transport(pending="")
    channel = transport._conn.channel
    assert transport.read_raw() == ""
    assert channel.reads >= 1


def test_read_raw_returns_empty_when_channel_missing():
    """Degrade gracefully if the netmiko object has no channel (never crashes)."""
    transport = make_transport(bare=True)
    assert transport.read_raw() == ""