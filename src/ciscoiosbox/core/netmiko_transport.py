"""Netmiko-backed transport covering SSH, Telnet and Serial.

Netmiko already abstracts all three behind one ``ConnectHandler`` (selected by
the ``_telnet`` / ``_serial`` device-type suffix), so a single implementation
serves every connection type. The value this module adds on top is:

* mapping netmiko/paramiko/pyserial exceptions onto our typed hierarchy so the
  UI can show an actionable message instead of a stack trace;
* detecting IOS-level rejections (``% Invalid input``) that netmiko happily
  returns as ordinary output;
* exposing the raw channel for the interactive terminal widget.
"""
from __future__ import annotations

import logging
import re
import socket
import time

from .exceptions import (
    AuthenticationError,
    ConnectionRefused,
    ConnectionTimeout,
    EnableFailed,
    InsufficientPrivilege,
    NotConnected,
    SerialPortError,
    SessionLost,
)
from .models import ConnectionType, DeviceInfo, DeviceProfile
from .transport import BaseTransport

log = logging.getLogger(__name__)


class NetmikoTransport(BaseTransport):
    """Concrete transport driving a device through netmiko."""

    def __init__(self, profile: DeviceProfile) -> None:
        super().__init__(profile)
        self._conn = None            # netmiko BaseConnection
        self._base_prompt = ""

    # ── connection arguments ──────────────────────────────────────────────────

    def _build_kwargs(self) -> dict:
        """Translate a :class:`DeviceProfile` into netmiko's kwargs."""
        p = self.profile
        kwargs: dict = {
            "device_type": p.netmiko_device_type,
            "username": p.username,
            "password": p.password,
            "secret": p.enable_password,
            "conn_timeout": p.conn_timeout,
            "read_timeout_override": None,
            "global_delay_factor": p.global_delay_factor,
            "fast_cli": p.fast_cli,
            # Netmiko's own auto-detect of paging can be slow on consoles; we
            # let session_preparation handle `terminal length 0`.
            "session_log": None,
        }

        if p.connection_type is ConnectionType.SERIAL:
            # Netmiko falls back to `host` when serial_settings lacks a port, so
            # set both to keep every netmiko version happy.
            kwargs["host"] = p.serial.port
            kwargs["serial_settings"] = {
                "port": p.serial.port,
                "baudrate": p.serial.baudrate,
                "bytesize": p.serial.bytesize,
                "parity": p.serial.parity,
                "stopbits": p.serial.stopbits,
                "xonxoff": p.serial.xonxoff,
                "rtscts": p.serial.rtscts,
                "timeout": 1,
            }
            # Serial logins often have no username at all; netmiko copes with "".
        else:
            kwargs["host"] = p.host
            kwargs["port"] = p.port

        if p.connection_type is ConnectionType.SSH:
            # Legacy IOS images negotiate old KEX/ciphers that modern paramiko
            # disables by default. Netmiko exposes this escape hatch.
            kwargs["disabled_algorithms"] = {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}
            kwargs["allow_agent"] = False
            kwargs["use_keys"] = False

        return kwargs

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> DeviceInfo:
        from netmiko import ConnectHandler
        from netmiko.exceptions import (
            NetmikoAuthenticationException,
            NetmikoTimeoutException,
        )

        kwargs = self._build_kwargs()
        log.info("Connecting to %s via %s", self.profile.display_target,
                 self.profile.connection_type.value)

        # A console that emits asynchronous syslog (e.g. a fan alert) can trip
        # netmiko's session preparation mid-connect; a fresh retry dials
        # through the noise.
        attempts = 3 if self.profile.connection_type is ConnectionType.SERIAL else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self._conn = ConnectHandler(**kwargs)
                last_exc = None
                break
            except NetmikoAuthenticationException as exc:
                raise AuthenticationError(detail=str(exc)) from exc
            except NetmikoTimeoutException as exc:
                if attempt + 1 < attempts and "Pattern not detected" in str(exc):
                    last_exc = exc
                    log.debug("Serial connect tripped on console noise, retrying "
                              "(%d/%d)", attempt + 1, attempts)
                    continue
                raise ConnectionTimeout(detail=str(exc)) from exc
            except (socket.timeout, TimeoutError) as exc:
                raise ConnectionTimeout(detail=str(exc)) from exc
            except ConnectionRefusedError as exc:
                raise ConnectionRefused(detail=str(exc)) from exc
            except socket.gaierror as exc:
                raise ConnectionTimeout(
                    f"Could not resolve host '{self.profile.host}'.", detail=str(exc)
                ) from exc
            except OSError as exc:
                # pyserial raises SerialException (an OSError subclass) for a
                # busy or missing port; distinguish it so the message can be
                # specific.
                if self.profile.connection_type is ConnectionType.SERIAL:
                    raise SerialPortError(detail=str(exc)) from exc
                raise ConnectionRefused(detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - netmiko raises assorted types
                msg = str(exc).lower()
                if "authentication" in msg or "password" in msg:
                    raise AuthenticationError(detail=str(exc)) from exc
                last_exc = exc
                if attempt + 1 < attempts and "Pattern not detected" in str(exc):
                    log.debug("Serial connect tripped by console noise (retry %d/%d)",
                              attempt + 1, attempts)
                    continue
                raise ConnectionRefused(
                    f"Connection to {self.profile.display_target} failed.",
                    detail=str(exc),
                ) from exc
        if self._conn is None:
            raise ConnectionRefused(
                f"Connection to {self.profile.display_target} failed.",
                detail=str(last_exc or "unknown error"))

        self._connected = True

        # A serial console may come up mid-session with no prompt until we send
        # a newline; nudge it so find_prompt() has something to latch onto.
        if self.profile.connection_type is ConnectionType.SERIAL:
            try:
                self._conn.write_channel("\r\n")
                time.sleep(0.4)
                self._conn.read_channel()
                self._conn.set_base_prompt()
            except Exception:  # noqa: BLE001 - best-effort wake-up
                log.debug("Serial prompt nudge failed", exc_info=True)

        # Console noise can leave netmiko's base_prompt pointing at a syslog
        # line instead of the real prompt; re-anchor it to a sane prompt.
        try:
            self._ensure_sane_prompt(self._conn)
            self._base_prompt = self._conn.base_prompt
        except Exception:  # noqa: BLE001
            self._base_prompt = ""

        # Escalate if an enable secret was supplied. A failure here is not fatal:
        # read-only monitoring still works, so we surface it as a warning later.
        enabled = False
        if self.profile.enable_password:
            try:
                enabled = self.enable()
            except EnableFailed:
                log.warning("Enable failed for %s", self.profile.name)
        else:
            try:
                enabled = bool(self._conn.check_enable_mode())
            except Exception:  # noqa: BLE001
                enabled = self._base_prompt.strip().endswith("#")

        return self._gather_facts(enabled)

    def _gather_facts(self, in_enable_mode: bool) -> DeviceInfo:
        """Run ``show version`` and parse it into :class:`DeviceInfo`."""
        from ..parsers.system import parse_show_version

        info = DeviceInfo(in_enable_mode=in_enable_mode)
        try:
            output = self.send_command("show version", read_timeout=30)
            info = parse_show_version(output)
            info.in_enable_mode = in_enable_mode
        except Exception:  # noqa: BLE001 - facts are advisory, never fatal
            log.debug("show version failed during fact gathering", exc_info=True)

        if not info.hostname:
            # Derive it from the prompt as a fallback (strip trailing >/#).
            info.hostname = self._base_prompt.rstrip("#>").strip() or self.profile.host
        return info

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.disconnect()
            except Exception:  # noqa: BLE001 - teardown must never raise
                log.debug("Error during disconnect", exc_info=True)
            finally:
                self._conn = None
        self._connected = False

    def is_alive(self) -> bool:
        if self._conn is None:
            return False
        # netmiko's is_alive() only handles telnet and SSH; for a serial
        # connection it falls through to the SSH branch and raises
        # AssertionError (a serial object has no paramiko transport), which we
        # would swallow and report as dead — killing every serial session at
        # the first keepalive tick. A serial line is alive while the port is
        # still open; trust that and probe the raw channel cheaply instead.
        if self.profile.connection_type is ConnectionType.SERIAL:
            try:
                conn = self._conn
                if conn.remote_conn is None or not getattr(conn.remote_conn, "is_open", False):
                    return False
                # A read probe doubles as the keepalive nudge.
                return True
            except Exception:  # noqa: BLE001 - never let a probe crash the loop
                return False
        try:
            return bool(self._conn.is_alive())
        except Exception:  # noqa: BLE001
            return False

    def _require_conn(self):
        if self._conn is None or not self._connected:
            raise NotConnected()
        return self._conn

    # ── structured commands ───────────────────────────────────────────────────

    #: A sane IOS prompt ends with > (user EXEC) or # (privileged/config).
    _PROMPT_RE = re.compile(r"^[A-Za-z0-9_.\-]+[>#]\s*$")

    def _ensure_sane_prompt(self, conn, attempts: int = 4) -> None:
        """Re-anchor netmiko's base_prompt to a real device prompt.

        On a console with asynchronous syslog output, ``find_prompt()`` can
        return one of those log lines (e.g. ``*Mar 28 23:53:58.225: %HARDWARE...``)
        instead of ``Switch>``, because it takes the last line of whatever it
        read.  Every later ``send_command`` then waits for that one-off log
        line to reappear and times out.

        Probe until the prompt looks like a prompt; if we cannot find one,
        fall back to netmiko's own base_prompt so the command still runs.
        """
        for _ in range(attempts):
            try:
                prompt = conn.find_prompt()
            except Exception:  # noqa: BLE001 - probe must never raise
                return
            if self._PROMPT_RE.match(prompt):
                if prompt != conn.base_prompt:
                    log.debug("Re-anchored base_prompt: %r -> %r",
                              conn.base_prompt, prompt)
                    conn.base_prompt = prompt
                return
            log.debug("Skipping non-prompt line from find_prompt(): %r", prompt)

    def send_command(
        self,
        command: str,
        *,
        read_timeout: float | None = None,
        expect_string: str | None = None,
    ) -> str:
        from netmiko.exceptions import ReadTimeout

        from ..parsers.errors import raise_for_ios_error

        conn = self._require_conn()
        timeout = read_timeout if read_timeout is not None else self.profile.read_timeout

        self._ensure_sane_prompt(conn)

        kwargs: dict = {"read_timeout": timeout}
        if expect_string:
            kwargs["expect_string"] = expect_string

        try:
            output = conn.send_command(command, **kwargs)
        except ReadTimeout as exc:
            raise ConnectionTimeout(
                f"'{command}' did not complete within {timeout:.0f}s.", detail=str(exc)
            ) from exc
        except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
            self._connected = False
            raise SessionLost(detail=str(exc)) from exc
        except OSError as exc:
            self._connected = False
            raise SessionLost(detail=str(exc)) from exc

        output = output if isinstance(output, str) else str(output)
        # Turn "% Invalid input" into a typed exception.
        raise_for_ios_error(command, output)
        return output

    def send_config(self, commands: list[str]) -> str:
        from netmiko.exceptions import ReadTimeout

        from ..parsers.errors import find_ios_error

        conn = self._require_conn()

        # Config mode is privileged; fail early with a clear message rather than
        # letting the device echo a cryptic rejection.
        try:
            if not conn.check_enable_mode():
                if self.profile.enable_password:
                    self.enable()
                else:
                    raise InsufficientPrivilege(command="; ".join(commands))
        except InsufficientPrivilege:
            raise
        except Exception:  # noqa: BLE001 - probe failure shouldn't block the attempt
            log.debug("check_enable_mode failed", exc_info=True)

        try:
            output = conn.send_config_set(
                commands, read_timeout=max(self.profile.read_timeout, 30.0)
            )
        except ReadTimeout as exc:
            raise ConnectionTimeout(
                "The configuration change did not complete in time.", detail=str(exc)
            ) from exc
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._connected = False
            raise SessionLost(detail=str(exc)) from exc

        # send_config_set does not raise on rejected lines — inspect the echo.
        problem = find_ios_error(output)
        if problem:
            raise problem.as_exception(command="; ".join(commands), output=output)
        return output

    def enable(self) -> bool:
        conn = self._require_conn()
        try:
            if conn.check_enable_mode():
                return True
            conn.enable()
            return bool(conn.check_enable_mode())
        except Exception as exc:  # noqa: BLE001 - netmiko raises ValueError here
            raise EnableFailed(detail=str(exc)) from exc

    def find_prompt(self) -> str:
        conn = self._require_conn()
        try:
            self._base_prompt = conn.find_prompt()
        except Exception as exc:  # noqa: BLE001
            raise SessionLost(detail=str(exc)) from exc
        return self._base_prompt

    def save_config(self) -> str:
        conn = self._require_conn()
        if not conn.check_enable_mode():
            if self.profile.enable_password:
                self.enable()
            else:
                raise InsufficientPrivilege(command="write memory")
        try:
            return conn.save_config()
        except Exception as exc:  # noqa: BLE001 - netmiko wraps several failures
            # Fall back to the explicit command if netmiko's helper trips up on
            # an unusual confirmation prompt.
            log.debug("save_config() helper failed, trying explicit copy", exc_info=True)
            return self.send_command(
                "copy running-config startup-config",
                expect_string=r"#",
                read_timeout=60,
            ) or str(exc)

    # ── raw channel ───────────────────────────────────────────────────────────

    def write_raw(self, data: str) -> None:
        conn = self._require_conn()
        try:
            conn.write_channel(data)
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._connected = False
            raise SessionLost(detail=str(exc)) from exc

    def read_raw(self, timeout: float = 0.1) -> str:
        """Drain whatever is buffered. Returns '' when the device is quiet.

        Non-blocking on purpose. ``read_channel()`` blocks on the underlying
        SSH/serial recv up to ``blocking_timeout`` (20s by default), which
        would wedge the connection worker: every keystroke and structured
        command queues behind a read that has nothing to return.

        ``read_buffer()`` performs a single recv that returns immediately when
        no data is available (paramiko's ``recv_ready`` / the serial
        ``in_waiting`` guard), so the worker's idle poll stays responsive.
        """
        conn = self._require_conn()
        channel = getattr(conn, "channel", None)
        try:
            if channel is None or conn.remote_conn is None:
                return ""
            data = channel.read_buffer()
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._connected = False
            raise SessionLost(detail=str(exc)) from exc
        return data or ""

    def resize_pty(self, cols: int, rows: int) -> None:
        """Resize the remote pseudo-terminal. Only meaningful over SSH."""
        if self.profile.connection_type is not ConnectionType.SSH:
            return
        conn = self._conn
        channel = getattr(conn, "remote_conn", None)
        if channel is None:
            return
        try:
            channel.resize_pty(width=max(20, cols), height=max(5, rows))
        except Exception:  # noqa: BLE001 - cosmetic only
            log.debug("resize_pty failed", exc_info=True)
