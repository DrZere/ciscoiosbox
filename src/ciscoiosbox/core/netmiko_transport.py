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

        try:
            self._conn = ConnectHandler(**kwargs)
        except NetmikoAuthenticationException as exc:
            raise AuthenticationError(detail=str(exc)) from exc
        except NetmikoTimeoutException as exc:
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
            # pyserial raises SerialException (an OSError subclass) for a busy or
            # missing port; distinguish it so the message can be specific.
            if self.profile.connection_type is ConnectionType.SERIAL:
                raise SerialPortError(detail=str(exc)) from exc
            raise ConnectionRefused(detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - netmiko raises assorted types
            msg = str(exc).lower()
            if "authentication" in msg or "password" in msg:
                raise AuthenticationError(detail=str(exc)) from exc
            raise ConnectionRefused(
                f"Connection to {self.profile.display_target} failed.", detail=str(exc)
            ) from exc

        self._connected = True

        # A serial console may come up mid-session with no prompt until we send a
        # newline; nudge it so find_prompt() has something to latch onto.
        if self.profile.connection_type is ConnectionType.SERIAL:
            try:
                self._conn.write_channel("\r\n")
                time.sleep(0.4)
                self._conn.read_channel()
                self._conn.set_base_prompt()
            except Exception:  # noqa: BLE001 - best-effort wake-up
                log.debug("Serial prompt nudge failed", exc_info=True)

        try:
            self._base_prompt = self._conn.find_prompt()
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
        try:
            return bool(self._conn.is_alive())
        except Exception:  # noqa: BLE001
            return False

    def _require_conn(self):
        if self._conn is None or not self._connected:
            raise NotConnected()
        return self._conn

    # ── structured commands ───────────────────────────────────────────────────

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
        # Turn "% Invalid input detected" into a typed exception.
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
        """Drain whatever is buffered. Returns '' when the device is quiet."""
        conn = self._require_conn()
        try:
            data = conn.read_channel()
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
