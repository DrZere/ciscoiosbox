"""Live monitoring: CPU, memory and per-interface throughput.

Two data sources, chosen per device:

* **SNMP** (preferred when configured) — cheap, and interface counters are true
  64-bit octet totals, so throughput is an exact delta over a known interval.
* **CLI** (always available) — parses ``show processes cpu`` and the 5-minute
  exponentially weighted rate from ``show interfaces``. Heavier on the device
  and the rates lag bursts, but needs no extra configuration.

Polling is driven by a ``QTimer`` on the GUI thread which merely *submits* a
task; the work itself always runs on the connection worker thread.
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer, Signal

from ..core.exceptions import CiscoIOSBoxError, SnmpError
from ..core.models import ResourceSample, TrafficSample
from ..core.snmp import OID, SnmpClient, index_of, snmp_available
from ..core.transport import BaseTransport
from ..parsers import system as parse_sys
from ..parsers.interfaces import normalise_name, parse_interface_rates
from .base import BaseService

log = logging.getLogger(__name__)


class MonitorService(BaseService):
    """Periodic resource and traffic sampling."""

    kinds = ("mon_resources", "mon_traffic", "mon_snmp_test", "mon_if_index")

    resource_sample = Signal(object)          # ResourceSample
    traffic_sample = Signal(object)           # TrafficSample
    #: Emitted once when the data source is settled, e.g. "SNMP v2c" or "CLI".
    source_changed = Signal(str)
    #: Non-fatal monitoring problem; shown inline in the graph pane, not as a dialog.
    monitor_warning = Signal(str)

    #: Stop polling after this many consecutive failures, so a device that has
    #: gone away does not generate an unbounded stream of error toasts.
    _MAX_CONSECUTIVE_FAILURES = 4

    def __init__(self, controller, parent=None) -> None:
        super().__init__(controller, parent)

        self._interval_ms = 5000
        self._watched_interface = ""
        self._use_snmp = False
        self._snmp: SnmpClient | None = None
        self._if_index_cache: dict[str, str] = {}

        #: Previous SNMP counters, for computing a rate: (timestamp, rx, tx).
        self._last_counters: tuple[float, int, int] | None = None
        self._failures = 0
        self._source_label = ""

        self._resource_timer = QTimer(self)
        self._resource_timer.timeout.connect(self._poll_resources)
        self._traffic_timer = QTimer(self)
        self._traffic_timer.timeout.connect(self._poll_traffic)

    # ── configuration ─────────────────────────────────────────────────────────

    def configure(self) -> None:
        """Decide between SNMP and CLI based on the profile, and announce it."""
        profile = self.controller.profile
        settings = profile.snmp

        if settings.enabled and profile.host and snmp_available():
            self._snmp = SnmpClient(profile.host, settings)
            self._use_snmp = True
            self._source_label = f"SNMP v{settings.version}"
        else:
            self._snmp = None
            self._use_snmp = False
            self._source_label = "CLI"
            if settings.enabled and not snmp_available():
                self.monitor_warning.emit(
                    "SNMP is enabled for this profile but pysnmp is not installed — "
                    "falling back to CLI polling.")
            elif settings.enabled and not profile.host:
                self.monitor_warning.emit(
                    "SNMP needs an IP address, which a serial session does not have — "
                    "falling back to CLI polling.")

        self.source_changed.emit(self._source_label)

    @property
    def source_label(self) -> str:
        return self._source_label

    def set_interval(self, seconds: float) -> None:
        """Change the poll period. Takes effect on the next tick."""
        self._interval_ms = max(1000, int(seconds * 1000))
        if self._resource_timer.isActive():
            self._resource_timer.setInterval(self._interval_ms)
        if self._traffic_timer.isActive():
            self._traffic_timer.setInterval(self._interval_ms)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start_resources(self) -> None:
        if not self._source_label:
            self.configure()
        self._failures = 0
        self._resource_timer.start(self._interval_ms)
        self._poll_resources()          # don't make the user wait for tick one

    def stop_resources(self) -> None:
        self._resource_timer.stop()

    def start_traffic(self, interface: str) -> None:
        """Begin watching one interface. Switching ports resets the rate baseline."""
        interface = normalise_name(interface)
        if interface != self._watched_interface:
            # The previous counters belong to a different port; a delta across
            # them would be meaningless.
            self._last_counters = None
        self._watched_interface = interface
        if not interface:
            self._traffic_timer.stop()
            return
        self._failures = 0
        self._traffic_timer.start(self._interval_ms)
        self._poll_traffic()

    def stop_traffic(self) -> None:
        self._traffic_timer.stop()
        self._watched_interface = ""
        self._last_counters = None

    def stop_all(self) -> None:
        self.stop_resources()
        self.stop_traffic()
        if self._snmp is not None:
            self._snmp.close()

    @property
    def is_running(self) -> bool:
        return self._resource_timer.isActive() or self._traffic_timer.isActive()

    # ── failure back-off ──────────────────────────────────────────────────────

    def _note_failure(self, message: str) -> None:
        self._failures += 1
        if self._failures >= self._MAX_CONSECUTIVE_FAILURES:
            self.stop_resources()
            self.stop_traffic()
            self.monitor_warning.emit(
                f"Monitoring stopped after {self._failures} consecutive failures. "
                f"Last error: {message}")
        else:
            log.debug("Monitor poll failed (%d/%d): %s",
                      self._failures, self._MAX_CONSECUTIVE_FAILURES, message)

    def _note_success(self) -> None:
        self._failures = 0

    # ── resource polling ──────────────────────────────────────────────────────

    def _poll_resources(self) -> None:
        if not self.controller.is_connected:
            return
        # Skip this tick if the previous one is still in flight — a slow device
        # must not accumulate a backlog of identical queries.
        if self.has_pending("mon_resources"):
            return

        snmp = self._snmp if self._use_snmp else None

        def work(transport: BaseTransport) -> ResourceSample:
            if snmp is not None:
                try:
                    return _snmp_resources(snmp)
                except SnmpError as exc:
                    log.debug("SNMP resource poll failed, using CLI: %s", exc)
            return _cli_resources(transport)

        self._submit(work, "mon_resources", self._on_resource,
                     self._on_monitor_error, priority=7, label="poll resources")

    def _on_resource(self, sample: ResourceSample) -> None:
        self._note_success()
        self.resource_sample.emit(sample)

    # ── traffic polling ───────────────────────────────────────────────────────

    def _poll_traffic(self) -> None:
        if not self.controller.is_connected or not self._watched_interface:
            return
        if self.has_pending("mon_traffic"):
            return

        interface = self._watched_interface
        snmp = self._snmp if self._use_snmp else None
        index = self._if_index_cache.get(interface, "")
        previous = self._last_counters

        def work(transport: BaseTransport) -> tuple[TrafficSample, str, tuple | None]:
            if snmp is not None:
                try:
                    return _snmp_traffic(snmp, interface, index, previous)
                except SnmpError as exc:
                    log.debug("SNMP traffic poll failed, using CLI: %s", exc)
            sample = _cli_traffic(transport, interface)
            return sample, index, None

        self._submit(work, "mon_traffic", self._on_traffic,
                     self._on_monitor_error, priority=7,
                     label=f"poll traffic {interface}")

    def _on_traffic(self, result: tuple[TrafficSample, str, tuple | None]) -> None:
        sample, if_index, counters = result
        self._note_success()
        if if_index:
            self._if_index_cache[sample.interface] = if_index
        if counters is not None:
            self._last_counters = counters
        # The first SNMP poll has no baseline, so its rate is meaningless —
        # record the counters but do not draw a spike at t=0.
        if sample.rx_bps < 0 or sample.tx_bps < 0:
            return
        self.traffic_sample.emit(sample)

    def _on_monitor_error(self, exc: CiscoIOSBoxError) -> None:
        self._note_failure(exc.user_message)

    # ── SNMP connectivity test (used by the profile dialog) ───────────────────

    def test_snmp(self) -> str:
        """Verify the SNMP settings by reading sysName."""
        profile = self.controller.profile
        if not snmp_available():
            self.error.emit("pysnmp is not installed, so SNMP cannot be tested.",
                            "mon_snmp_test")
            return ""
        client = SnmpClient(profile.host, profile.snmp)

        def work(_: BaseTransport) -> str:
            return client.test()

        return self._submit(
            work, "mon_snmp_test",
            lambda name: self.monitor_warning.emit(f"SNMP OK — device reports '{name}'."),
            label="snmp test")


# ─── Collection helpers (run on the worker thread) ────────────────────────────

def _cli_resources(transport: BaseTransport) -> ResourceSample:
    """Read CPU and memory over the CLI."""
    cpu_output = ""
    mem_output = ""

    # `| include` keeps the response tiny, which matters at a 5s cadence.
    for command in ("show processes cpu | include CPU utilization",
                    "show processes cpu | include CPU util",
                    "show system resources"):
        try:
            cpu_output = transport.send_command(command, read_timeout=15)
            if cpu_output.strip():
                break
        except CiscoIOSBoxError:
            continue

    for command in ("show processes memory | include Processor Pool",
                    "show memory statistics",
                    "show system resources"):
        try:
            mem_output = transport.send_command(command, read_timeout=15)
            if mem_output.strip():
                break
        except CiscoIOSBoxError:
            continue

    return parse_sys.build_resource_sample(cpu_output, mem_output)


def _snmp_resources(client: SnmpClient) -> ResourceSample:
    """Read CPU and memory over SNMP."""
    sample = ResourceSample(timestamp=time.time())

    # CISCO-PROCESS-MIB is indexed per CPU; average across all of them.
    try:
        five_sec = client.walk(OID.CPM_CPU_5SEC, max_rows=16)
        one_min = client.walk(OID.CPM_CPU_1MIN, max_rows=16)
        five_min = client.walk(OID.CPM_CPU_5MIN, max_rows=16)

        def average(values: dict) -> float:
            numbers = [float(v) for v in values.values() if isinstance(v, (int, float))]
            return sum(numbers) / len(numbers) if numbers else 0.0

        sample.cpu_5sec = average(five_sec)
        sample.cpu_1min = average(one_min)
        sample.cpu_5min = average(five_min)
    except SnmpError:
        sample.cpu_5sec = sample.cpu_1min = sample.cpu_5min = 0.0

    if sample.cpu_1min == 0.0:
        # Fall back to OLD-CISCO-CPU-MIB scalars.
        try:
            values = client.get(OID.AVG_BUSY_5SEC, OID.AVG_BUSY_1MIN, OID.AVG_BUSY_5MIN)
            sample.cpu_5sec = float(values.get(OID.AVG_BUSY_5SEC, 0) or 0)
            sample.cpu_1min = float(values.get(OID.AVG_BUSY_1MIN, 0) or 0)
            sample.cpu_5min = float(values.get(OID.AVG_BUSY_5MIN, 0) or 0)
        except SnmpError:
            pass

    # CISCO-MEMORY-POOL-MIB: sum the Processor pool(s).
    try:
        names = client.walk(OID.MEM_POOL_NAME, max_rows=32)
        used = client.walk(OID.MEM_POOL_USED, max_rows=32)
        free = client.walk(OID.MEM_POOL_FREE, max_rows=32)

        processor_indices = {
            index_of(oid, OID.MEM_POOL_NAME)
            for oid, value in names.items()
            if "processor" in str(value).lower()
        } or {index_of(oid, OID.MEM_POOL_NAME) for oid in names}

        sample.mem_used_bytes = sum(
            int(v) for oid, v in used.items()
            if index_of(oid, OID.MEM_POOL_USED) in processor_indices
            and isinstance(v, int))
        sample.mem_free_bytes = sum(
            int(v) for oid, v in free.items()
            if index_of(oid, OID.MEM_POOL_FREE) in processor_indices
            and isinstance(v, int))
    except SnmpError:
        pass

    return sample


def _cli_traffic(transport: BaseTransport, interface: str) -> TrafficSample:
    """Read throughput from ``show interfaces <name>``."""
    output = transport.send_command(f"show interfaces {interface}", read_timeout=20)
    data = parse_interface_rates(output)
    return TrafficSample(
        timestamp=time.time(),
        interface=interface,
        rx_bps=float(data["rx_bps"]),
        tx_bps=float(data["tx_bps"]),
        rx_pps=float(data["rx_pps"]),
        tx_pps=float(data["tx_pps"]),
        rx_octets=int(data["rx_octets"]),
        tx_octets=int(data["tx_octets"]),
        bandwidth_bps=float(data["bandwidth_bps"]),
    )


def _resolve_if_index(client: SnmpClient, interface: str) -> str:
    """Map an interface name to its ifIndex by walking ifName, then ifDescr."""
    target = normalise_name(interface).lower()
    for base in (OID.IF_NAME, OID.IF_DESCR):
        try:
            rows = client.walk(base, max_rows=1024)
        except SnmpError:
            continue
        for oid, value in rows.items():
            if normalise_name(str(value)).lower() == target:
                return index_of(oid, base)
    raise SnmpError(f"Could not find '{interface}' in the device's interface table.")


def _snmp_traffic(client: SnmpClient, interface: str, if_index: str,
                  previous: tuple[float, int, int] | None
                  ) -> tuple[TrafficSample, str, tuple[float, int, int]]:
    """Read 64-bit octet counters and convert them into a bits/sec rate."""
    if not if_index:
        if_index = _resolve_if_index(client, interface)

    oids = (
        f"{OID.IF_HC_IN_OCTETS}.{if_index}",
        f"{OID.IF_HC_OUT_OCTETS}.{if_index}",
        f"{OID.IF_HIGH_SPEED}.{if_index}",
    )
    values = client.get(*oids)
    now = time.time()

    rx_octets = int(values.get(oids[0], 0) or 0)
    tx_octets = int(values.get(oids[1], 0) or 0)
    speed_mbps = float(values.get(oids[2], 0) or 0)

    sample = TrafficSample(
        timestamp=now,
        interface=interface,
        rx_octets=rx_octets,
        tx_octets=tx_octets,
        bandwidth_bps=speed_mbps * 1e6,
    )

    if previous is not None:
        last_time, last_rx, last_tx = previous
        elapsed = now - last_time
        if elapsed > 0:
            delta_rx = rx_octets - last_rx
            delta_tx = tx_octets - last_tx
            # A negative delta means the counter wrapped or the device rebooted.
            # 64-bit counters effectively never wrap, so treat it as a reset and
            # skip this interval rather than drawing a huge false spike.
            if delta_rx >= 0 and delta_tx >= 0:
                sample.rx_bps = delta_rx * 8.0 / elapsed
                sample.tx_bps = delta_tx * 8.0 / elapsed
            else:
                sample.rx_bps = sample.tx_bps = -1.0     # signals "discard"
    else:
        # First sample establishes the baseline only.
        sample.rx_bps = sample.tx_bps = -1.0

    return sample, if_index, (now, rx_octets, tx_octets)
