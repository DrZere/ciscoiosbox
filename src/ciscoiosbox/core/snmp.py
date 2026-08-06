"""SNMP v2c / v3 client for lightweight monitoring.

pysnmp 6 dropped its synchronous API, so this module owns a private asyncio
event loop on a dedicated daemon thread and exposes ordinary blocking
``get``/``walk`` methods on top of it. Callers (the monitor service, running on
a connection worker thread) therefore never need to know asyncio exists.

pysnmp is an optional dependency: :attr:`SnmpClient.available` reports whether
it imported, and the UI hides SNMP options when it did not.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from .exceptions import SnmpError, SnmpTimeout
from .models import SnmpSettings

log = logging.getLogger(__name__)


# ─── Useful OIDs ──────────────────────────────────────────────────────────────

class OID:
    """Named OIDs for the values this application graphs."""

    # SNMPv2-MIB / system
    SYS_DESCR = "1.3.6.1.2.1.1.1.0"
    SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
    SYS_NAME = "1.3.6.1.2.1.1.5.0"

    # CISCO-PROCESS-MIB — CPU averages
    CPM_CPU_5SEC = "1.3.6.1.4.1.9.9.109.1.1.1.1.6"
    CPM_CPU_1MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.7"
    CPM_CPU_5MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.8"

    # OLD-CISCO-CPU-MIB — fallback for platforms without CISCO-PROCESS-MIB
    AVG_BUSY_5SEC = "1.3.6.1.4.1.9.2.1.56.0"
    AVG_BUSY_1MIN = "1.3.6.1.4.1.9.2.1.57.0"
    AVG_BUSY_5MIN = "1.3.6.1.4.1.9.2.1.58.0"

    # CISCO-MEMORY-POOL-MIB
    MEM_POOL_NAME = "1.3.6.1.4.1.9.9.48.1.1.1.2"
    MEM_POOL_USED = "1.3.6.1.4.1.9.9.48.1.1.1.5"
    MEM_POOL_FREE = "1.3.6.1.4.1.9.9.48.1.1.1.6"

    # IF-MIB — interface table
    IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
    IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
    IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
    IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
    IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
    IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
    IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"      # in Mbit/s
    # 64-bit counters; essential above ~100 Mbit/s where 32-bit ones wrap fast.
    IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
    IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
    IF_IN_UCAST_PKTS = "1.3.6.1.2.1.2.2.1.11"
    IF_OUT_UCAST_PKTS = "1.3.6.1.2.1.2.2.1.17"


def snmp_available() -> bool:
    """True when pysnmp can be imported."""
    try:
        import pysnmp  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class _LoopThread:
    """A daemon thread running a private asyncio loop, shared by all clients."""

    _instance: _LoopThread | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run, name="snmp-asyncio", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @classmethod
    def instance(cls) -> _LoopThread:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro, timeout: float):
        """Run a coroutine on the private loop and block for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise SnmpTimeout(detail="the asyncio call exceeded its deadline") from exc


class SnmpClient:
    """Blocking SNMP client for one device."""

    def __init__(self, host: str, settings: SnmpSettings) -> None:
        self.host = host
        self.settings = settings
        self._engine = None
        self._auth = None
        self._target = None

    @property
    def available(self) -> bool:
        return snmp_available()

    # ── pysnmp object construction ────────────────────────────────────────────

    def _build_auth(self):
        """Return a CommunityData (v2c) or UsmUserData (v3) credential object."""
        from pysnmp.hlapi.v3arch.asyncio import CommunityData, UsmUserData

        if self.settings.version == "2c":
            if not self.settings.community:
                raise SnmpError("No SNMP community string is configured.")
            # mpModel=1 selects SNMPv2c (0 would be v1).
            return CommunityData(self.settings.community, mpModel=1)

        from pysnmp.hlapi.v3arch.asyncio import (
            usmAesCfb128Protocol, usmAesCfb192Protocol, usmAesCfb256Protocol,
            usmDESPrivProtocol, usmHMAC128SHA224AuthProtocol,
            usmHMAC192SHA256AuthProtocol, usmHMAC256SHA384AuthProtocol,
            usmHMAC384SHA512AuthProtocol, usmHMACMD5AuthProtocol,
            usmHMACSHAAuthProtocol, usmNoAuthProtocol, usmNoPrivProtocol,
        )

        auth_protocols = {
            "MD5": usmHMACMD5AuthProtocol, "SHA": usmHMACSHAAuthProtocol,
            "SHA224": usmHMAC128SHA224AuthProtocol,
            "SHA256": usmHMAC192SHA256AuthProtocol,
            "SHA384": usmHMAC256SHA384AuthProtocol,
            "SHA512": usmHMAC384SHA512AuthProtocol,
        }
        priv_protocols = {
            "DES": usmDESPrivProtocol, "AES128": usmAesCfb128Protocol,
            "AES192": usmAesCfb192Protocol, "AES256": usmAesCfb256Protocol,
        }

        s = self.settings
        if not s.username:
            raise SnmpError("No SNMP v3 username is configured.")

        # Degrade cleanly across noAuthNoPriv / authNoPriv / authPriv.
        auth_proto = auth_protocols.get(s.auth_protocol, usmNoAuthProtocol) \
            if s.auth_key else usmNoAuthProtocol
        priv_proto = priv_protocols.get(s.priv_protocol, usmNoPrivProtocol) \
            if s.priv_key else usmNoPrivProtocol

        return UsmUserData(
            s.username,
            authKey=s.auth_key or None,
            privKey=s.priv_key or None,
            authProtocol=auth_proto,
            privProtocol=priv_proto,
        )

    async def _build_target(self):
        """Create a UDP transport target, tolerating both pysnmp 6.x shapes."""
        from pysnmp.hlapi.v3arch.asyncio import UdpTransportTarget

        address = (self.host, self.settings.port)
        timeout = self.settings.timeout
        retries = self.settings.retries

        # pysnmp >= 6.2 exposes an async `create()`; older releases use __init__.
        creator = getattr(UdpTransportTarget, "create", None)
        if creator is not None:
            result = creator(address, timeout=timeout, retries=retries)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return UdpTransportTarget(address, timeout=timeout, retries=retries)

    def _ensure_engine(self):
        from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

        if self._engine is None:
            self._engine = SnmpEngine()
            self._auth = self._build_auth()
        return self._engine

    @property
    def _deadline(self) -> float:
        """Wall-clock budget for one operation, allowing for pysnmp retries."""
        return self.settings.timeout * (self.settings.retries + 1) + 2.0

    # ── public API ────────────────────────────────────────────────────────────

    def get(self, *oids: str) -> dict[str, Any]:
        """GET one or more OIDs. Returns {oid: python value}."""
        try:
            return _LoopThread.instance().run(self._get(oids), self._deadline + 2.0)
        except (SnmpError, SnmpTimeout):
            raise
        except ImportError as exc:
            raise SnmpError("pysnmp is not installed.", detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SnmpError("The SNMP GET failed.", detail=str(exc)) from exc

    async def _get(self, oids: tuple[str, ...]) -> dict[str, Any]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData, ObjectIdentity, ObjectType, get_cmd,
        )

        engine = self._ensure_engine()
        target = await self._build_target()
        objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]

        error_indication, error_status, _, var_binds = await get_cmd(
            engine, self._auth, target, ContextData(), *objects)

        if error_indication:
            text = str(error_indication)
            if "timeout" in text.lower() or "no response" in text.lower():
                raise SnmpTimeout(detail=text)
            raise SnmpError("The SNMP GET failed.", detail=text)
        if error_status:
            raise SnmpError(f"The device returned an SNMP error: {error_status.prettyPrint()}")

        return {str(name): _to_python(value) for name, value in var_binds}

    def walk(self, base_oid: str, *, max_rows: int = 512) -> dict[str, Any]:
        """Walk a subtree. Returns {full_oid: value}, keyed by index suffix."""
        try:
            # A walk issues many round trips; scale the deadline with row count.
            budget = self._deadline + min(60.0, max_rows * 0.05)
            return _LoopThread.instance().run(self._walk(base_oid, max_rows), budget)
        except (SnmpError, SnmpTimeout):
            raise
        except ImportError as exc:
            raise SnmpError("pysnmp is not installed.", detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SnmpError("The SNMP walk failed.", detail=str(exc)) from exc

    async def _walk(self, base_oid: str, max_rows: int) -> dict[str, Any]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData, ObjectIdentity, ObjectType, bulk_walk_cmd,
        )

        engine = self._ensure_engine()
        target = await self._build_target()
        results: dict[str, Any] = {}

        # non-repeaters=0, max-repetitions=25 is the usual sweet spot for GETBULK.
        iterator = bulk_walk_cmd(
            engine, self._auth, target, ContextData(), 0, 25,
            ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False)

        async for error_indication, error_status, _, var_binds in iterator:
            if error_indication:
                text = str(error_indication)
                if "timeout" in text.lower() or "no response" in text.lower():
                    raise SnmpTimeout(detail=text)
                raise SnmpError("The SNMP walk failed.", detail=text)
            if error_status:
                raise SnmpError(
                    f"The device returned an SNMP error: {error_status.prettyPrint()}")
            for name, value in var_binds:
                results[str(name)] = _to_python(value)
            if len(results) >= max_rows:
                break

        return results

    def test(self) -> str:
        """Verify reachability. Returns the device's sysName on success."""
        values = self.get(OID.SYS_NAME, OID.SYS_DESCR)
        return str(values.get(OID.SYS_NAME, "")) or "(no sysName)"

    def close(self) -> None:
        """Release the SNMP engine's transport dispatcher."""
        if self._engine is not None:
            try:
                self._engine.close_dispatcher()
            except Exception:  # noqa: BLE001 - older pysnmp spells it differently
                try:
                    self._engine.transportDispatcher.closeDispatcher()
                except Exception:  # noqa: BLE001
                    log.debug("Could not close SNMP dispatcher", exc_info=True)
            self._engine = None


def _to_python(value: Any) -> Any:
    """Convert a pysnmp rfc1902 object into a plain int / str."""
    try:
        # Integer-ish types expose an exact int conversion.
        from pysnmp.proto.rfc1902 import Counter32, Counter64, Gauge32, Integer, Integer32

        if isinstance(value, (Counter32, Counter64, Gauge32, Integer, Integer32)):
            return int(value)
    except Exception:  # noqa: BLE001
        pass

    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    try:
        text = value.prettyPrint()
    except AttributeError:
        text = str(value)
    return text


def index_of(oid: str, base: str) -> str:
    """Return the row index of ``oid`` within ``base`` (``'...1.5.10'`` → ``'10'``)."""
    base = base.rstrip(".")
    if oid.startswith(base + "."):
        return oid[len(base) + 1:]
    return oid.rsplit(".", 1)[-1]
