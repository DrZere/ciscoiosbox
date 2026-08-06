"""Connection threading.

Design
------
Each connected device owns exactly one :class:`ConnectionWorker` running on one
``QThread``. Every interaction — terminal keystrokes, ``show`` commands, config
pushes — is submitted as a :class:`Task` onto that worker's queue.

Because a single thread services the queue sequentially, access to the device
channel is serialised *by construction*. There are no locks around the
transport, and it is impossible for a structured command and a terminal
keystroke to interleave mid-read.

The worker never touches widgets. It communicates upward purely through Qt
signals, which Qt marshals onto the GUI thread automatically.
"""
from __future__ import annotations

import logging
import queue
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from .exceptions import CiscoIOSBoxError, NotConnected, SessionLost
from .models import ConnectionState, DeviceInfo, DeviceProfile
from .transport import BaseTransport

log = logging.getLogger(__name__)

#: Executed on the worker thread with the live transport as its only argument.
TaskFn = Callable[[BaseTransport], Any]


@dataclass(order=True)
class Task:
    """A unit of work for the worker thread.

    ``priority`` orders the queue: lower runs first. Interactive keystrokes use
    priority 0 so typing stays responsive even while a slow poll is queued.
    """

    priority: int
    sequence: int
    fn: TaskFn = field(compare=False)
    task_id: str = field(compare=False, default_factory=lambda: uuid.uuid4().hex)
    label: str = field(compare=False, default="")
    #: Tag used by views to route results without matching on task_id.
    kind: str = field(compare=False, default="")


class ConnectionWorker(QObject):
    """Owns a transport and drives it on a dedicated thread.

    All slots on this object must be invoked via queued connections (or by the
    thread itself); never call them directly from the GUI thread.
    """

    # ── signals (worker thread → GUI thread) ──────────────────────────────────
    state_changed = Signal(object)                 # ConnectionState
    connected = Signal(object)                     # DeviceInfo
    disconnected = Signal()
    #: (task_id, kind, result) — result type depends on the task.
    task_succeeded = Signal(str, str, object)
    #: (task_id, kind, exception)
    task_failed = Signal(str, str, object)
    #: Raw decoded text from the device, for the terminal widget.
    data_received = Signal(str)
    #: Non-fatal notice worth surfacing in the status bar.
    notice = Signal(str)
    #: Emitted when the session drops unexpectedly, carrying the reason.
    session_lost = Signal(str)

    #: How long the run loop blocks waiting for a task before polling the
    #: channel. Small enough to feel instant, large enough to stay near-idle.
    _QUEUE_POLL_INTERVAL = 0.03

    def __init__(self, profile: DeviceProfile, transport_factory=None) -> None:
        super().__init__()
        self.profile = profile
        self._transport_factory = transport_factory or self._default_factory
        self._transport: BaseTransport | None = None

        self._queue: queue.PriorityQueue[Task] = queue.PriorityQueue()
        self._sequence = 0
        self._running = False
        self._stop_requested = False
        #: When True the run loop drains the channel and emits data_received.
        self._streaming = True
        self._state = ConnectionState.DISCONNECTED
        self._last_keepalive = 0.0

    @staticmethod
    def _default_factory(profile: DeviceProfile) -> BaseTransport:
        from .netmiko_transport import NetmikoTransport

        return NetmikoTransport(profile)

    # ── state helpers ─────────────────────────────────────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state)

    # ── task submission (safe to call from the GUI thread) ────────────────────

    def submit(self, fn: TaskFn, *, kind: str = "", label: str = "", priority: int = 5) -> str:
        """Queue work for the device. Returns the task id for result routing.

        Thread-safe: ``queue.PriorityQueue`` handles the cross-thread handoff,
        so views may call this directly.
        """
        self._sequence += 1
        task = Task(priority=priority, sequence=self._sequence, fn=fn, kind=kind, label=label)
        self._queue.put(task)
        return task.task_id

    def set_streaming(self, enabled: bool) -> None:
        """Enable/disable draining the channel into ``data_received``.

        Disabled while the terminal tab is hidden to avoid buffering output
        nobody is looking at.
        """
        self._streaming = enabled

    # ── slots invoked on the worker thread ────────────────────────────────────

    @Slot()
    def run(self) -> None:
        """Thread entry point: connect, then service the queue until stopped."""
        self._running = True
        self._stop_requested = False
        self._set_state(ConnectionState.CONNECTING)

        try:
            self._transport = self._transport_factory(self.profile)
            info: DeviceInfo = self._transport.connect()
        except CiscoIOSBoxError as exc:
            self._set_state(ConnectionState.FAILED)
            self.task_failed.emit("connect", "connect", exc)
            self._running = False
            return
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            log.exception("Unexpected error while connecting")
            self._set_state(ConnectionState.FAILED)
            self.task_failed.emit("connect", "connect", CiscoIOSBoxError(
                "An unexpected error occurred while connecting.", detail=str(exc)))
            self._running = False
            return

        self._set_state(ConnectionState.CONNECTED)
        self.connected.emit(info)
        if self.profile.enable_password and not info.in_enable_mode:
            self.notice.emit(
                "Connected, but privileged EXEC mode is unavailable — "
                "configuration changes will fail."
            )

        self._loop()
        self._teardown()

    def _loop(self) -> None:
        """Alternate between running queued tasks and draining the channel."""
        while not self._stop_requested:
            try:
                task = self._queue.get(timeout=self._QUEUE_POLL_INTERVAL)
            except queue.Empty:
                task = None

            if task is not None:
                self._execute(task)
                continue

            # Idle: pump the terminal stream and run the keepalive probe.
            if self._streaming:
                try:
                    data = self._transport.read_raw()
                    if data:
                        self.data_received.emit(data)
                except SessionLost as exc:
                    self._handle_session_lost(exc)
                    return
                except Exception:  # noqa: BLE001 - a bad read must not kill the loop
                    log.debug("Channel read failed", exc_info=True)

            if not self._keepalive_ok():
                return

    def _execute(self, task: Task) -> None:
        """Run one task, converting any failure into ``task_failed``."""
        if self._transport is None or not self._transport.is_connected:
            self.task_failed.emit(task.task_id, task.kind, NotConnected())
            return
        try:
            result = task.fn(self._transport)
        except SessionLost as exc:
            self.task_failed.emit(task.task_id, task.kind, exc)
            self._handle_session_lost(exc)
        except CiscoIOSBoxError as exc:
            self.task_failed.emit(task.task_id, task.kind, exc)
        except Exception as exc:  # noqa: BLE001 - surface as a typed error
            log.exception("Task %s (%s) raised", task.label or task.task_id, task.kind)
            self.task_failed.emit(
                task.task_id, task.kind,
                CiscoIOSBoxError("The operation failed unexpectedly.", detail=str(exc)),
            )
        else:
            self.task_succeeded.emit(task.task_id, task.kind, result)

    def _keepalive_ok(self) -> bool:
        """Probe liveness every 20s; returns False once the session is gone."""
        now = time.monotonic()
        if now - self._last_keepalive < 20.0:
            return True
        self._last_keepalive = now
        if self._transport is not None and not self._transport.is_alive():
            self._handle_session_lost(SessionLost())
            return False
        return True

    def _handle_session_lost(self, exc: Exception) -> None:
        message = getattr(exc, "user_message", str(exc))
        log.warning("Session lost: %s", message)
        self._set_state(ConnectionState.FAILED)
        self.session_lost.emit(message)
        self._stop_requested = True

    def _teardown(self) -> None:
        """Close the transport and drain any tasks still queued."""
        if self._transport is not None:
            try:
                self._transport.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("Transport teardown error", exc_info=True)
            self._transport = None

        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            self.task_failed.emit(pending.task_id, pending.kind, NotConnected())

        self._running = False
        self._set_state(ConnectionState.DISCONNECTED)
        self.disconnected.emit()

    @Slot()
    def stop(self) -> None:
        """Ask the loop to exit. Safe to call from any thread."""
        self._stop_requested = True

    @property
    def is_running(self) -> bool:
        """True until the run loop and teardown have both finished."""
        return self._running


# ── background thread reaping ─────────────────────────────────────────────────
#
# ``ConnectionController.shutdown()`` must not block the GUI thread: the worker
# exits its run loop within one poll interval (~30ms) of seeing the stop
# request, but transport teardown — netmiko's graceful SSH close in particular
# — can take seconds on a slow or unresponsive device. Joining synchronously
# froze the UI (and multiplied per-tab at app quit).
#
# Instead we hand the join to a _ConnectionReaper. It holds the QThread and the
# worker alive until the thread finishes, which matters more than it sounds:
# both are parentless QObjects whose only strong references live in the
# controller. If the tab that owned them is destroyed while the thread still
# runs (tab close is immediate now), the wrappers would be garbage-collected
# and Qt aborts with "QThread destroyed while still running". The reaper also
# terminates a wedged transport after a grace period so nothing leaks forever.

_retiring: set["_ConnectionReaper"] = set()


class _ConnectionReaper(QObject):
    """Join a connection thread off the GUI thread.

    Guards the thread and worker until the thread has finished so their owning
    tab can be destroyed immediately, then releases both and deletes itself.
    """

    def __init__(self, thread: QThread, worker: ConnectionWorker,
                 grace_ms: int) -> None:
        super().__init__()
        self._thread = thread
        self._worker = worker
        self.settled = False
        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._force_terminate)
        self._grace.start(grace_ms)
        thread.finished.connect(self._on_thread_finished)

    def _on_thread_finished(self) -> None:
        if self.settled:
            return
        self.settled = True
        self._grace.stop()
        self.deleteLater()

    def _force_terminate(self) -> None:
        if self._thread.isRunning():
            # A wedged blocking read can outlive the grace period; terminate is
            # the last resort so the thread never leaks. Same policy as the old
            # synchronous shutdown, just applied in the background.
            log.warning("Connection thread did not stop cleanly; terminating")
            self._thread.terminate()
            self._thread.wait(1000)
        if not self._thread.isRunning():
            self._on_thread_finished()
        # else: leave the reaper parked. Holding the references keeps a
        # still-running QThread alive rather than destroying it mid-run, which
        # would abort the process.


def _retire_connection(thread: QThread, worker: ConnectionWorker,
                       grace_ms: int) -> _ConnectionReaper:
    """Park a connection thread and its worker until the thread has ended."""
    reaper = _ConnectionReaper(thread, worker, grace_ms)
    _retiring.add(reaper)
    reaper.destroyed.connect(lambda: _retiring.discard(reaper))
    return reaper


class ConnectionController(QObject):
    """GUI-thread facade over a worker and its thread.

    Views talk to this object only. It owns thread lifecycle, so nothing above
    it has to reason about ``QThread`` semantics.
    """

    state_changed = Signal(object)
    connected = Signal(object)                 # DeviceInfo
    disconnected = Signal()
    task_succeeded = Signal(str, str, object)
    task_failed = Signal(str, str, object)
    data_received = Signal(str)
    notice = Signal(str)
    session_lost = Signal(str)

    def __init__(self, profile: DeviceProfile, parent: QObject | None = None,
                 transport_factory=None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.device_info = DeviceInfo()
        self._reaper: _ConnectionReaper | None = None

        self._thread = QThread()
        self._thread.setObjectName(f"conn-{profile.name}")
        self._worker = ConnectionWorker(profile, transport_factory=transport_factory)
        self._worker.moveToThread(self._thread)

        # Thread starts → worker.run() begins on that thread.
        self._thread.started.connect(self._worker.run)

        # Re-emit worker signals. Qt queues these across the thread boundary,
        # so every handler downstream executes on the GUI thread.
        self._worker.state_changed.connect(self.state_changed)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.task_succeeded.connect(self.task_succeeded)
        self._worker.task_failed.connect(self.task_failed)
        self._worker.data_received.connect(self.data_received)
        self._worker.notice.connect(self.notice)
        self._worker.session_lost.connect(self.session_lost)

        # Once the worker's loop returns, wind the thread down.
        self._worker.disconnected.connect(self._thread.quit)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._thread.isRunning():
            self._thread.start()

    def shutdown(self, wait_ms: int = 4000) -> None:
        """Stop the worker and reap its thread without blocking the GUI thread.

        The worker notices the stop request within one poll interval (~30ms),
        but transport teardown can take seconds on a slow device. Joining
        synchronously froze the UI, so the join is handed to a
        :class:`_ConnectionReaper` which keeps the thread and worker alive
        until they finish (destroying the tab early cannot abort a running
        ``QThread``) and falls back to ``terminate()`` if a transport wedges.
        Idempotent.

        ``wait_ms`` is now the background grace period before terminate, not a
        synchronous join budget.
        """
        self._worker.stop()
        if not self._thread.isRunning():
            return
        if self._reaper is not None and not self._reaper.settled:
            return
        self._thread.quit()
        self._reaper = _retire_connection(self._thread, self._worker, wait_ms)

    @property
    def thread_running(self) -> bool:
        """True while the worker thread is still alive (used at app quit)."""
        return self._thread.isRunning()

    @property
    def state(self) -> ConnectionState:
        return self._worker.state

    @property
    def is_connected(self) -> bool:
        return self._worker.state is ConnectionState.CONNECTED

    def _on_connected(self, info: DeviceInfo) -> None:
        self.device_info = info
        self.connected.emit(info)

    # ── work submission ───────────────────────────────────────────────────────

    def submit(self, fn: TaskFn, *, kind: str = "", label: str = "", priority: int = 5) -> str:
        if not self._worker.is_running:
            # The worker is already wound down: reject immediately with the
            # same typed failure teardown would have produced, instead of
            # queueing work nobody will ever run.
            task_id = uuid.uuid4().hex
            self.task_failed.emit(task_id, kind, NotConnected())
            return task_id
        return self._worker.submit(fn, kind=kind, label=label, priority=priority)

    def send_keys(self, data: str) -> str:
        """Forward terminal keystrokes at the highest priority."""
        return self.submit(
            lambda t: t.write_raw(data), kind="terminal_write",
            label="keystroke", priority=0,
        )

    def set_streaming(self, enabled: bool) -> None:
        self._worker.set_streaming(enabled)
