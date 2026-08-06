"""Threading and service-layer tests using a fake transport.

The architectural promise of this application is that no network I/O ever runs
on the GUI thread. These tests assert that mechanically rather than by
inspection.
"""
from __future__ import annotations

import threading
import time

import pytest

from ciscoiosbox.core.exceptions import (
    AuthenticationError, InvalidInputError, NotConnected,
)
from ciscoiosbox.core.models import ConnectionState, DeviceInfo, DeviceProfile
from ciscoiosbox.core.transport import BaseTransport

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

from ciscoiosbox.core.connection import ConnectionController  # noqa: E402
from ciscoiosbox.services.interface_service import InterfaceService  # noqa: E402

IP_BRIEF = """\
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet1/0/1   unassigned      YES unset  up                    up
GigabitEthernet1/0/2   unassigned      YES unset  administratively down down
"""

STATUS = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   uplink             connected    trunk        full   1000 10/100/1000BaseTX
Gi1/0/2   spare              disabled     10           auto   auto 10/100/1000BaseTX
"""


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class FakeTransport(BaseTransport):
    """Records the threads it was touched by, and can simulate failures."""

    def __init__(self, profile: DeviceProfile, *, fail_connect: Exception | None = None,
                 latency: float = 0.01) -> None:
        super().__init__(profile)
        self.threads_used: set[int] = set()
        self.commands: list[str] = []
        self.config_sets: list[list[str]] = []
        self._fail_connect = fail_connect
        self._latency = latency

    def _record(self) -> None:
        self.threads_used.add(threading.get_ident())

    def connect(self) -> DeviceInfo:
        self._record()
        if self._fail_connect is not None:
            raise self._fail_connect
        time.sleep(self._latency)
        self._connected = True
        return DeviceInfo(hostname="sw-test", model="WS-C2960X", version="15.2")

    def disconnect(self) -> None:
        self._connected = False

    def is_alive(self) -> bool:
        return self._connected

    def send_command(self, command, *, read_timeout=None, expect_string=None):
        self._record()
        self.commands.append(command)
        time.sleep(self._latency)
        if command.startswith("show ip interface brief"):
            return IP_BRIEF
        if command == "show interfaces status":
            return STATUS
        if "bogus" in command:
            raise InvalidInputError(command=command)
        return ""

    def send_config(self, commands):
        self._record()
        self.config_sets.append(list(commands))
        return ""

    def enable(self):
        return True

    def find_prompt(self):
        return "sw-test#"

    def save_config(self):
        return "OK"

    def write_raw(self, data):
        self._record()

    def read_raw(self, timeout=0.1):
        return ""

    def resize_pty(self, cols, rows):
        pass


def make_controller(**kwargs):
    """Build a controller wired to a fresh FakeTransport."""
    created: list[FakeTransport] = []

    def factory(profile):
        transport = FakeTransport(profile, **kwargs)
        created.append(transport)
        return transport

    profile = DeviceProfile(name="test", host="10.0.0.1", username="admin")
    return ConnectionController(profile, transport_factory=factory), created


def spin(condition, timeout: float = 5.0) -> bool:
    """Run the Qt event loop until ``condition()`` is true or time runs out."""
    loop = QEventLoop()
    deadline = time.monotonic() + timeout

    def check():
        if condition() or time.monotonic() > deadline:
            loop.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(5)
    loop.exec()
    timer.stop()
    return condition()


# ─── Threading guarantees ─────────────────────────────────────────────────────

def test_network_io_never_runs_on_the_calling_thread(qapp):
    """The core architectural promise: the GUI thread issues no device I/O."""
    main_thread = threading.get_ident()
    controller, created = make_controller()

    connected = []
    controller.connected.connect(lambda info: connected.append(info))
    controller.start()

    assert spin(lambda: bool(connected)), "never connected"

    controller.submit(lambda t: t.send_command("show ip interface brief"),
                      kind="probe")
    assert spin(lambda: bool(created[0].commands))

    controller.shutdown()

    transport = created[0]
    assert main_thread not in transport.threads_used
    # Exactly one thread ever touches the transport, which is what makes the
    # lock-free design safe.
    assert len(transport.threads_used) == 1


def test_signals_are_delivered_on_the_receiving_thread(qapp):
    """Qt must marshal worker signals back for the UI to consume safely."""
    main_thread = threading.get_ident()
    controller, _ = make_controller()

    observed: list[int] = []
    controller.connected.connect(lambda info: observed.append(threading.get_ident()))
    controller.start()

    assert spin(lambda: bool(observed))
    controller.shutdown()

    assert observed == [main_thread]


def test_event_loop_stays_responsive_during_slow_io(qapp):
    """A slow device must not stall the UI."""
    controller, _ = make_controller(latency=0.12)

    ticks = [0]
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start(5)

    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    for _ in range(5):
        controller.submit(lambda t: t.send_command("show version"), kind="probe")
    spin(lambda: False, timeout=0.8)

    timer.stop()
    controller.shutdown()
    assert ticks[0] > 10, "the event loop was blocked by device I/O"


# ─── Lifecycle ────────────────────────────────────────────────────────────────

def test_connection_failure_is_reported_not_raised(qapp):
    controller, _ = make_controller(
        fail_connect=AuthenticationError())

    failures: list[tuple] = []
    controller.task_failed.connect(
        lambda task_id, kind, exc: failures.append((kind, exc)))
    controller.start()

    assert spin(lambda: bool(failures))
    controller.shutdown()

    kind, exc = failures[0]
    assert kind == "connect"
    assert isinstance(exc, AuthenticationError)
    assert "Authentication failed" in exc.user_message


def test_tasks_queued_after_shutdown_fail_cleanly(qapp):
    """Pending work must be drained with NotConnected, never left hanging."""
    controller, _ = make_controller()
    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    controller.shutdown()

    failures = []
    controller.task_failed.connect(
        lambda task_id, kind, exc: failures.append(exc))
    controller.submit(lambda t: t.send_command("show version"), kind="late")
    spin(lambda: bool(failures), timeout=1.0)
    # Either it is rejected outright or never runs; what must not happen is a
    # silent success against a closed transport.
    assert all(isinstance(f, NotConnected) for f in failures)


def test_state_transitions(qapp):
    controller, _ = make_controller()
    states: list[ConnectionState] = []
    controller.state_changed.connect(states.append)

    controller.start()
    assert spin(lambda: ConnectionState.CONNECTED in states)
    controller.shutdown()
    spin(lambda: ConnectionState.DISCONNECTED in states, timeout=2.0)

    assert states[0] is ConnectionState.CONNECTING
    assert ConnectionState.CONNECTED in states


# ─── Service layer ────────────────────────────────────────────────────────────

def test_interface_service_merges_and_emits(qapp):
    controller, created = make_controller()
    service = InterfaceService(controller)

    rows = []
    service.interfaces_loaded.connect(rows.extend)

    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    service.refresh()
    assert spin(lambda: bool(rows))
    controller.shutdown()

    assert len(rows) == 2
    by_name = {r.name: r for r in rows}
    assert by_name["GigabitEthernet1/0/1"].description == "uplink"
    assert by_name["GigabitEthernet1/0/2"].is_shutdown is True


def test_set_admin_state_issues_config_and_verifies(qapp):
    """The service must read the state back rather than assume it applied."""
    controller, created = make_controller()
    service = InterfaceService(controller)

    changes = []
    service.admin_state_changed.connect(
        lambda name, shut: changes.append((name, shut)))

    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    service.set_admin_state("GigabitEthernet1/0/2", False)
    assert spin(lambda: bool(changes))
    controller.shutdown()

    transport = created[0]
    assert ["interface GigabitEthernet1/0/2", "no shutdown", "exit"] \
        in transport.config_sets
    # A verification read must follow the write.
    assert any(c.startswith("show ip interface brief GigabitEthernet1/0/2")
               for c in transport.commands)


def test_service_errors_surface_as_signals(qapp):
    controller, _ = make_controller()
    service = InterfaceService(controller)

    errors = []
    service.error.connect(lambda message, kind: errors.append((kind, message)))

    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    service.load_detail("bogus0/0")
    assert spin(lambda: bool(errors))
    controller.shutdown()

    kind, message = errors[0]
    assert kind == "intf_detail"
    assert "invalid input" in message.lower()


def test_has_pending_tracks_kinds(qapp):
    """Pollers rely on this to avoid queueing duplicate work."""
    controller, _ = make_controller(latency=0.2)
    service = InterfaceService(controller)

    connected = []
    controller.connected.connect(lambda i: connected.append(i))
    controller.start()
    spin(lambda: bool(connected))

    assert not service.has_pending("intf_refresh")
    service.refresh()
    assert service.has_pending("intf_refresh")

    loaded = []
    service.interfaces_loaded.connect(loaded.append)
    spin(lambda: bool(loaded))
    assert not service.has_pending("intf_refresh")
    controller.shutdown()
