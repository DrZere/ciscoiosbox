"""Service-layer base class.

A service is the ViewModel in this architecture. It owns no widgets and does no
blocking work: it builds a callable, hands it to the
:class:`~ciscoiosbox.core.connection.ConnectionController`, and re-emits the
parsed result as a typed Qt signal.

Each service filters ``task_succeeded`` / ``task_failed`` by the ``kind`` tags
it registered, so several services can share one controller without seeing each
other's traffic.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..core.connection import ConnectionController
from ..core.exceptions import CiscoIOSBoxError
from ..core.transport import BaseTransport

log = logging.getLogger(__name__)


class BaseService(QObject):
    """Common plumbing for device-facing services."""

    #: (user_message, kind) for any failed task this service owns.
    error = Signal(str, str)
    #: True while at least one of this service's tasks is in flight.
    busy_changed = Signal(bool)

    #: Subclasses list the task kinds they handle.
    kinds: tuple[str, ...] = ()

    def __init__(self, controller: ConnectionController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        #: task_id → kind for every task this service has in flight.
        self._pending: dict[str, str] = {}
        #: task_id → handler, so results route without a chain of if/elif.
        self._handlers: dict[str, Callable[[Any], None]] = {}
        self._error_handlers: dict[str, Callable[[CiscoIOSBoxError], None]] = {}

        controller.task_succeeded.connect(self._on_succeeded)
        controller.task_failed.connect(self._on_failed)

    # ── submission ────────────────────────────────────────────────────────────

    def _submit(
        self,
        fn: Callable[[BaseTransport], Any],
        kind: str,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[CiscoIOSBoxError], None] | None = None,
        *,
        priority: int = 5,
        label: str = "",
    ) -> str:
        """Queue work and register per-call result handlers."""
        task_id = self.controller.submit(fn, kind=kind, label=label or kind, priority=priority)
        if on_success is not None:
            self._handlers[task_id] = on_success
        if on_error is not None:
            self._error_handlers[task_id] = on_error
        self._mark_busy(task_id, kind)
        return task_id

    def _mark_busy(self, task_id: str, kind: str | None) -> None:
        """Track a task as started (``kind`` given) or finished (``kind`` None)."""
        was_busy = bool(self._pending)
        if kind is not None:
            self._pending[task_id] = kind
        else:
            self._pending.pop(task_id, None)
        if bool(self._pending) != was_busy:
            self.busy_changed.emit(bool(self._pending))

    @property
    def is_busy(self) -> bool:
        return bool(self._pending)

    def has_pending(self, kind: str) -> bool:
        """True when a task of this kind is already queued or running.

        Used by pollers to skip a tick rather than let a slow device build up a
        backlog of identical queries.
        """
        return kind in self._pending.values()

    # ── result routing ────────────────────────────────────────────────────────

    def _owns(self, kind: str) -> bool:
        return kind in self.kinds

    def _on_succeeded(self, task_id: str, kind: str, result: Any) -> None:
        if not self._owns(kind):
            return
        self._mark_busy(task_id, None)
        self._error_handlers.pop(task_id, None)
        handler = self._handlers.pop(task_id, None)
        if handler is None:
            return
        try:
            handler(result)
        except Exception as exc:  # noqa: BLE001 - a bad handler must not break the app
            log.exception("Handler for %s failed", kind)
            self.error.emit(f"Could not process the device's response: {exc}", kind)

    def _on_failed(self, task_id: str, kind: str, exc: object) -> None:
        if not self._owns(kind):
            return
        self._mark_busy(task_id, None)
        self._handlers.pop(task_id, None)
        handler = self._error_handlers.pop(task_id, None)

        error = exc if isinstance(exc, CiscoIOSBoxError) else CiscoIOSBoxError(str(exc))
        if handler is not None:
            try:
                handler(error)
                return
            except Exception:  # noqa: BLE001
                log.exception("Error handler for %s failed", kind)
        self.error.emit(error.user_message, kind)
