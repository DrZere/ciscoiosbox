#!/usr/bin/env python3
"""Run CiscoIOSBox against a simulated Cisco switch — no real hardware needed.

    python demo.py                  launch the app with two simulated devices
    python demo.py --screenshots    render each tab to demo_screenshots/

The simulator is *stateful*: shutting a port really marks it down, creating a
VLAN really adds it to the database, and the interactive terminal answers
commands. CPU and traffic figures drift over time so the graphs actually move.

This exists because the useful thing to verify about a device manager is the
interaction loop, and you should not need a switch on your desk to see it.
"""
from __future__ import annotations

import math
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ciscoiosbox.core.exceptions import InvalidInputError  # noqa: E402
from ciscoiosbox.core.models import DeviceInfo, DeviceProfile  # noqa: E402
from ciscoiosbox.core.transport import BaseTransport  # noqa: E402


# ─── Simulated device state ───────────────────────────────────────────────────

class FakePort:
    """One switchport, with the state the real commands would report."""

    def __init__(self, name: str, description: str = "", vlan: str = "1",
                 mode: str = "access", connected: bool = True,
                 shutdown: bool = False, speed: str = "1000",
                 duplex: str = "full") -> None:
        self.name = name
        self.description = description
        self.vlan = vlan
        self.mode = mode
        self.connected = connected
        self.shutdown = shutdown
        self.speed = speed
        self.duplex = duplex
        # Baseline load so each port's graph looks different.
        self.load = random.uniform(0.02, 0.55)

    @property
    def short(self) -> str:
        return self.name.replace("GigabitEthernet", "Gi").replace("Vlan", "Vl")

    @property
    def status(self) -> str:
        if self.shutdown:
            return "disabled"
        return "connected" if self.connected else "notconnect"

    @property
    def ip_brief_status(self) -> str:
        if self.shutdown:
            return "administratively down"
        return "up" if self.connected else "down"


class FakeSwitch:
    """A small stateful IOS switch that answers the commands this app sends."""

    def __init__(self, hostname: str = "sw-access-01",
                 model: str = "WS-C2960X-24TS-L") -> None:
        self.hostname = hostname
        self.model = model
        self.version = "15.2(4)E7"
        self.serial = "FOC1934X2QL"
        self.started = time.time()
        self.saved = True

        self.ports: list[FakePort] = [
            FakePort("GigabitEthernet1/0/1", "uplink to core sw", "trunk", "trunk"),
            FakePort("GigabitEthernet1/0/2", "wifi ap - floor 2", "20"),
            FakePort("GigabitEthernet1/0/3", "printer - accounts", "30", speed="100",
                     duplex="full"),
            FakePort("GigabitEthernet1/0/4", "desk 4a", "10"),
            FakePort("GigabitEthernet1/0/5", "desk 4b", "10", connected=False),
            FakePort("GigabitEthernet1/0/6", "", "1", connected=False),
            FakePort("GigabitEthernet1/0/7", "spare port", "1", connected=False,
                     shutdown=True),
            FakePort("GigabitEthernet1/0/8", "conf room tv", "20"),
            FakePort("GigabitEthernet1/0/9", "", "1", connected=False),
            FakePort("GigabitEthernet1/0/10", "server - backup nic", "40"),
            FakePort("GigabitEthernet1/0/11", "old cctv nvr", "50", connected=False,
                     shutdown=True),
            FakePort("GigabitEthernet1/0/12", "link to switch-b", "trunk", "trunk"),
        ]
        self.svi = FakePort("Vlan10", "management", "10")
        self.mgmt_ip = "192.168.10.2"
        self.mgmt_mask = "255.255.255.0"
        self.gateway = "192.168.10.1"

        self.vlans: dict[int, tuple[str, str]] = {
            1: ("default", "active"),
            10: ("MGMT", "active"),
            20: ("WIFI", "active"),
            30: ("PRINTERS", "active"),
            40: ("SERVERS", "active"),
            50: ("CCTV", "active"),
            1002: ("fddi-default", "act/unsup"),
            1003: ("token-ring-default", "act/unsup"),
        }

    # ── helpers ───────────────────────────────────────────────────────────────

    def port(self, name: str) -> FakePort | None:
        target = name.lower().replace(" ", "")
        for port in [*self.ports, self.svi]:
            if port.name.lower() == target or port.short.lower() == target:
                return port
        # Accept abbreviations like "gi1/0/1".
        from ciscoiosbox.parsers.interfaces import normalise_name

        canonical = normalise_name(name).lower()
        for port in [*self.ports, self.svi]:
            if port.name.lower() == canonical:
                return port
        return None

    @property
    def uptime(self) -> str:
        seconds = int(time.time() - self.started) + 7_412_900
        weeks, rest = divmod(seconds, 604800)
        days, rest = divmod(rest, 86400)
        hours, rest = divmod(rest, 3600)
        return f"{weeks} weeks, {days} days, {hours} hours, {rest // 60} minutes"

    def cpu(self) -> tuple[int, int, int]:
        """A wandering CPU figure so the graph is not a flat line."""
        now = time.time()
        base = 14 + 9 * math.sin(now / 11.0) + 4 * math.sin(now / 3.1)
        spike = 22 if (int(now) % 47) < 2 else 0        # occasional burst
        five = max(1, min(99, int(base + spike + random.uniform(-2, 2))))
        return five, max(1, int(base + 1)), max(1, int(base - 1))

    def traffic(self, port: FakePort) -> tuple[int, int, int, int]:
        """Return (rx_bps, tx_bps, rx_pps, tx_pps) for a port."""
        if port.shutdown or not port.connected:
            return 0, 0, 0, 0
        capacity = 1e9 if port.speed == "1000" else 1e8
        now = time.time()
        wobble = 0.5 + 0.5 * math.sin(now / 7.0 + port.load * 10)
        rx = capacity * port.load * wobble * random.uniform(0.85, 1.15)
        tx = rx * random.uniform(0.25, 0.6)
        return int(rx), int(tx), int(rx / 8000), int(tx / 8000)

    # ── show commands ─────────────────────────────────────────────────────────

    def run(self, command: str) -> str:
        command = command.strip()
        low = command.lower()

        if low.startswith("show version"):
            return self._show_version()
        if low.startswith("show ip interface brief"):
            parts = command.split()
            return self._ip_brief(parts[4] if len(parts) > 4 else "")
        if low.startswith("show interfaces status"):
            return self._interfaces_status()
        if low.startswith("show interfaces switchport"):
            return self._switchport()
        if low.startswith("show interfaces description"):
            return self._descriptions()
        if low.startswith("show interfaces"):
            return self._interface_detail(command.split()[-1])
        if low.startswith("show vlan brief"):
            return self._vlan_brief()
        if low.startswith("show vlan id"):
            return self._vlan_brief(only=int(command.split()[-1]))
        if low.startswith("show processes cpu"):
            five, one, five_min = self.cpu()
            return (f"CPU utilization for five seconds: {five}%/0%; "
                    f"one minute: {one}%; five minutes: {five_min}%")
        if low.startswith("show processes memory"):
            used = 74_253_464 + random.randint(-900_000, 900_000)
            return (f"Processor Pool Total: 212933812 Used: {used} "
                    f"Free: {212933812 - used}")
        if low.startswith("show memory statistics"):
            return ("                Head    Total(b)     Used(b)     Free(b)\n"
                    "Processor   2A3B4C5D   212933812    74253464   138680348")
        if low.startswith("show running-config interface"):
            return self._interface_config(command.split()[-1])
        if "include ^hostname" in low:
            return f"hostname {self.hostname}"
        if "default-gateway" in low or "route 0.0.0.0" in low:
            return f"ip default-gateway {self.gateway}"
        if low.startswith("show running-config"):
            return self._running_config()
        if low.startswith("show startup-config"):
            return self._running_config() if self.saved else "!\n!\n!"

        raise InvalidInputError(
            "The device did not recognise part of that command.", command=command)

    def _show_version(self) -> str:
        return (
            f"Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), "
            f"Version {self.version}, RELEASE SOFTWARE (fc2)\n"
            f"Technical Support: http://www.cisco.com/techsupport\n"
            f"Copyright (c) 1986-2018 by Cisco Systems, Inc.\n\n"
            f"{self.hostname} uptime is {self.uptime}\n"
            f"System returned to ROM by power-on\n"
            f'System image file is "flash:/c2960x-universalk9-mz.152-4.E7.bin"\n\n'
            f"cisco {self.model} (APM86XXX) processor (revision H0) with "
            f"131072K bytes of memory.\n"
            f"Processor board ID {self.serial}\n"
            f"System serial number            : {self.serial}\n"
            f"Model Number                    : {self.model}\n")

    def _ip_brief(self, only: str = "") -> str:
        lines = ["Interface              IP-Address      OK? Method Status"
                 "                Protocol"]
        candidates = [self.svi, *self.ports]
        if only:
            match = self.port(only)
            candidates = [match] if match else []
        for port in candidates:
            ip = self.mgmt_ip if port is self.svi else "unassigned"
            lines.append(
                f"{port.name:<22} {ip:<15} YES NVRAM  "
                f"{port.ip_brief_status:<21} "
                f"{'up' if port.ip_brief_status == 'up' else 'down'}")
        return "\n".join(lines)

    def _interfaces_status(self) -> str:
        lines = ["Port      Name               Status       Vlan       "
                 "Duplex  Speed Type"]
        for port in self.ports:
            lines.append(
                f"{port.short:<9} {port.description[:18]:<18} {port.status:<12} "
                f"{port.vlan:<10} {port.duplex:>6} {port.speed:>5} "
                f"10/100/1000BaseTX")
        return "\n".join(lines)

    def _switchport(self) -> str:
        blocks = []
        for port in self.ports:
            mode = "trunk" if port.mode == "trunk" else "static access"
            blocks.append(
                f"Name: {port.short}\nSwitchport: Enabled\n"
                f"Administrative Mode: {mode}\nOperational Mode: {mode}\n"
                f"Access Mode VLAN: {port.vlan if port.mode != 'trunk' else '1'}\n")
        return "\n".join(blocks)

    def _descriptions(self) -> str:
        lines = ["Interface                      Status         Protocol Description"]
        for port in self.ports:
            lines.append(f"{port.short:<30} {port.ip_brief_status:<14} "
                         f"{'up':<8} {port.description}")
        return "\n".join(lines)

    def _interface_detail(self, name: str) -> str:
        port = self.port(name)
        if port is None:
            raise InvalidInputError(command=f"show interfaces {name}")
        rx, tx, rx_pps, tx_pps = self.traffic(port)
        bandwidth = 1000000 if port.speed == "1000" else 100000
        state = "administratively down" if port.shutdown else (
            "up" if port.connected else "down")
        protocol = "up (connected)" if port.connected and not port.shutdown else "down"
        return (
            f"{port.name} is {state}, line protocol is {protocol}\n"
            f"  Hardware is Gigabit Ethernet, address is 00c1.b1a2.03{random.randint(10,99)}\n"
            f"  Description: {port.description}\n"
            f"  MTU 1500 bytes, BW {bandwidth} Kbit/sec, DLY 10 usec,\n"
            f"  {port.duplex.title()}-duplex, {port.speed}Mb/s, "
            f"media type is 10/100/1000BaseTX\n"
            f"  5 minute input rate {rx} bits/sec, {rx_pps} packets/sec\n"
            f"  5 minute output rate {tx} bits/sec, {tx_pps} packets/sec\n"
            f"     {random.randint(10**8, 10**9)} packets input, "
            f"{random.randint(10**11, 10**12)} bytes, 0 no buffer\n"
            f"     {random.randint(10**8, 10**9)} packets output, "
            f"{random.randint(10**11, 10**12)} bytes, 0 underruns\n")

    def _vlan_brief(self, only: int | None = None) -> str:
        lines = ["VLAN Name                             Status    Ports",
                 "---- -------------------------------- --------- "
                 "-------------------------------"]
        for vlan_id, (name, status) in sorted(self.vlans.items()):
            if only is not None and vlan_id != only:
                continue
            members = [p.short for p in self.ports
                       if p.mode != "trunk" and p.vlan == str(vlan_id)]
            first = ", ".join(members[:4])
            lines.append(f"{vlan_id:<4} {name:<32} {status:<9} {first}")
            # Wrap the rest onto continuation lines, exactly as IOS does.
            for index in range(4, len(members), 4):
                lines.append(" " * 48 + ", ".join(members[index:index + 4]))
        return "\n".join(lines)

    def _interface_config(self, name: str) -> str:
        port = self.port(name)
        if port is None:
            raise InvalidInputError(command=name)
        lines = [f"interface {port.name}"]
        if port.description:
            lines.append(f" description {port.description}")
        if port is self.svi:
            lines.append(f" ip address {self.mgmt_ip} {self.mgmt_mask}")
        elif port.mode == "trunk":
            lines.append(" switchport mode trunk")
        else:
            lines.append(f" switchport access vlan {port.vlan}")
            lines.append(" switchport mode access")
        if port.shutdown:
            lines.append(" shutdown")
        lines.append("end")
        return "\n".join(lines)

    def _running_config(self) -> str:
        parts = [
            "Building configuration...\n",
            f"Current configuration : 8214 bytes\n!",
            "version 15.2", "no service pad", "service timestamps debug datetime msec",
            f"hostname {self.hostname}", "!", "boot-start-marker", "boot-end-marker",
            "!", "no aaa new-model", "system mtu routing 1500", "!",
            "spanning-tree mode pvst", "spanning-tree extend system-id", "!",
        ]
        for vlan_id, (name, _) in sorted(self.vlans.items()):
            if vlan_id in (1, 1002, 1003):
                continue
            parts += [f"vlan {vlan_id}", f" name {name}", "!"]
        for port in self.ports:
            parts.append(f"interface {port.name}")
            if port.description:
                parts.append(f" description {port.description}")
            if port.mode == "trunk":
                parts.append(" switchport mode trunk")
            else:
                parts += [f" switchport access vlan {port.vlan}",
                          " switchport mode access"]
            if port.shutdown:
                parts.append(" shutdown")
            parts.append("!")
        parts += [
            f"interface Vlan10", " description management",
            f" ip address {self.mgmt_ip} {self.mgmt_mask}", "!",
            f"ip default-gateway {self.gateway}", "!",
            "line con 0", " logging synchronous", "line vty 0 4",
            " login local", " transport input ssh", "!", "end",
        ]
        return "\n".join(parts)

    # ── configuration commands ────────────────────────────────────────────────

    def configure(self, commands: list[str]) -> str:
        """Apply config lines, mutating state the way the device would."""
        current: FakePort | None = None
        current_vlan: int | None = None
        echo = ["Enter configuration commands, one per line.  End with CNTL/Z."]

        for line in commands:
            line = line.strip()
            low = line.lower()

            if low.startswith("interface "):
                current = self.port(line.split(None, 1)[1])
                current_vlan = None
                if current is None:
                    echo.append("% Invalid input detected at '^' marker.")
                continue

            if low.startswith("vlan "):
                try:
                    current_vlan = int(line.split()[1])
                except (IndexError, ValueError):
                    echo.append("% Invalid input detected at '^' marker.")
                    continue
                if 1002 <= current_vlan <= 1005:
                    echo.append(f"% VLAN {current_vlan} is reserved")
                    current_vlan = None
                    continue
                self.vlans.setdefault(current_vlan, (f"VLAN{current_vlan:04d}",
                                                     "active"))
                current = None
                self.saved = False
                continue

            if low.startswith("no vlan "):
                vlan_id = int(line.split()[-1])
                self.vlans.pop(vlan_id, None)
                for port in self.ports:
                    if port.vlan == str(vlan_id):
                        port.vlan = "1"
                        port.connected = False
                self.saved = False
                continue

            if current_vlan is not None and low.startswith("name "):
                status = self.vlans[current_vlan][1]
                self.vlans[current_vlan] = (line.split(None, 1)[1], status)
                self.saved = False
                continue

            if low.startswith("hostname "):
                self.hostname = line.split(None, 1)[1]
                self.saved = False
                continue

            if current is not None:
                self._apply_interface_line(current, line, low, echo)
                continue

            if low in ("exit", "end", "!"):
                continue

        return "\n".join(echo)

    def _apply_interface_line(self, port: FakePort, line: str, low: str,
                              echo: list[str]) -> None:
        self.saved = False

        if low == "shutdown":
            port.shutdown = True
        elif low == "no shutdown":
            port.shutdown = False
            # Coming out of shutdown, a port with something plugged in comes up.
            port.connected = True
        elif low.startswith("description "):
            port.description = line.split(None, 1)[1]
        elif low == "no description":
            port.description = ""
        elif low.startswith("switchport access vlan "):
            vlan_id = line.split()[-1]
            if int(vlan_id) not in self.vlans:
                echo.append(f"% Access VLAN does not exist. Creating vlan {vlan_id}")
                self.vlans[int(vlan_id)] = (f"VLAN{int(vlan_id):04d}", "active")
            port.vlan = vlan_id
        elif low == "switchport mode access":
            port.mode = "access"
            if port.vlan == "trunk":
                port.vlan = "1"
        elif low == "switchport mode trunk":
            port.mode = "trunk"
            port.vlan = "trunk"
        elif low.startswith("switchport trunk encapsulation"):
            pass
        elif low.startswith("switchport voice vlan"):
            pass
        elif low.startswith("switchport trunk"):
            pass
        elif low.startswith("ip address dhcp"):
            self.mgmt_ip = "192.168.10.57"
        elif low.startswith("ip address "):
            parts = line.split()
            if len(parts) >= 4:
                self.mgmt_ip, self.mgmt_mask = parts[2], parts[3]
        elif low.startswith("speed ") or low.startswith("duplex "):
            value = line.split()[1]
            if low.startswith("speed"):
                port.speed = value
            else:
                port.duplex = value


# ─── Transport wrapping the simulator ─────────────────────────────────────────

class DemoTransport(BaseTransport):
    """A :class:`BaseTransport` backed by :class:`FakeSwitch`.

    Also implements a small line-buffered shell so the Terminal tab is genuinely
    interactive: keystrokes echo, backspace works, and commands return output.
    """

    def __init__(self, profile: DeviceProfile, switch: FakeSwitch | None = None,
                 latency: float = 0.12) -> None:
        super().__init__(profile)
        self.switch = switch or FakeSwitch()
        self._latency = latency
        self._pending = ""          # bytes waiting to be read back by the terminal
        self._line = ""             # current command line being typed
        self._in_config = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> DeviceInfo:
        time.sleep(0.5)             # make the "Connecting…" state visible
        self._connected = True
        self._pending = (
            f"\r\n{self.switch.hostname} line 1\r\n\r\n"
            f"{self.switch.hostname}#")
        from ciscoiosbox.parsers.system import parse_show_version

        info = parse_show_version(self.switch.run("show version"))
        info.in_enable_mode = True
        return info

    def disconnect(self) -> None:
        self._connected = False

    def is_alive(self) -> bool:
        return self._connected

    # ── structured commands ───────────────────────────────────────────────────

    def send_command(self, command, *, read_timeout=None, expect_string=None) -> str:
        time.sleep(self._latency)
        return self.switch.run(command)

    def send_config(self, commands: list[str]) -> str:
        time.sleep(self._latency * 1.5)
        return self.switch.configure(commands)

    def enable(self) -> bool:
        return True

    def find_prompt(self) -> str:
        return f"{self.switch.hostname}#"

    def save_config(self) -> str:
        time.sleep(0.4)
        self.switch.saved = True
        return "Building configuration...\n[OK]"

    # ── interactive shell ─────────────────────────────────────────────────────

    def write_raw(self, data: str) -> None:
        """Echo keystrokes and execute on carriage return."""
        for char in data:
            if char in "\r\n":
                self._pending += "\r\n"
                self._execute_line(self._line.strip())
                self._line = ""
            elif char in ("\x7f", "\x08"):
                if self._line:
                    self._line = self._line[:-1]
                    self._pending += "\b \b"
            elif char == "\x03":                       # Ctrl-C
                self._pending += "^C\r\n" + self._prompt()
                self._line = ""
            elif char == "\t":
                self._pending += ""                    # no completion in the demo
            elif char.isprintable():
                self._line += char
                self._pending += char

    def _prompt(self) -> str:
        return f"{self.switch.hostname}(config)#" if self._in_config \
            else f"{self.switch.hostname}#"

    def _execute_line(self, line: str) -> None:
        if not line:
            self._pending += self._prompt()
            return

        low = line.lower()

        if low in ("configure terminal", "conf t", "config t"):
            self._in_config = True
            self._pending += ("Enter configuration commands, one per line.  "
                             "End with CNTL/Z.\r\n" + self._prompt())
            return
        if self._in_config and low in ("exit", "end"):
            self._in_config = False
            self._pending += self._prompt()
            return
        if low in ("exit", "quit", "logout"):
            self._pending += "\r\n"
            self._connected = False
            return
        if low in ("clear", "cls"):
            self._pending += "\x1b[2J" + self._prompt()
            return

        if self._in_config:
            output = self.switch.configure([line])
            # Only surface rejections; successful config lines echo nothing.
            errors = [ln for ln in output.splitlines() if ln.startswith("%")]
            if errors:
                self._pending += "\r\n".join(errors) + "\r\n"
            self._pending += self._prompt()
            return

        if low in ("write memory", "wr", "copy running-config startup-config"):
            self.switch.saved = True
            self._pending += "Building configuration...\r\n[OK]\r\n" + self._prompt()
            return

        try:
            output = self.switch.run(line)
        except InvalidInputError:
            # Reproduce IOS's caret marker pointing at the offending token.
            caret = " " * (len(self._prompt()) + len(line.split()[0]) + 1)
            self._pending += (f"{caret}^\r\n"
                              f"% Invalid input detected at '^' marker.\r\n\r\n"
                              + self._prompt())
            return

        self._pending += output.replace("\n", "\r\n") + "\r\n" + self._prompt()

    def read_raw(self, timeout: float = 0.1) -> str:
        data, self._pending = self._pending, ""
        return data

    def resize_pty(self, cols: int, rows: int) -> None:
        pass


# ─── Demo wiring ──────────────────────────────────────────────────────────────

def build_store():
    """A throwaway session store holding a few simulated devices."""
    from ciscoiosbox.core.credentials import CredentialStore, NullBackend
    from ciscoiosbox.core.models import ConnectionType
    from ciscoiosbox.core.session_store import SessionStore

    directory = Path(tempfile.mkdtemp(prefix="ciscoiosbox-demo-"))
    store = SessionStore(path=directory / "sessions.json",
                         credentials=CredentialStore(NullBackend()))

    access = DeviceProfile(
        name="Access Switch 01", host="192.168.10.2", username="admin",
        group="Branch Office", notes="Simulated 2960X — safe to experiment on.")
    access.password = "demo"
    access.enable_password = "demo"

    core = DeviceProfile(
        name="Core Switch", host="192.168.10.1", username="admin",
        group="Datacenter", notes="Simulated 3850.")
    core.password = "demo"
    core.enable_password = "demo"

    console = DeviceProfile(
        name="Console (serial)", connection_type=ConnectionType.SERIAL,
        group="Branch Office", notes="Simulated console session.")
    console.serial.port = "/dev/tty.usbserial-DEMO"

    for profile in (access, core, console):
        store.add(profile)
    return store


#: Each profile gets its own switch, so state persists per device.
_SWITCHES: dict[str, FakeSwitch] = {}


def demo_transport_factory(profile: DeviceProfile) -> DemoTransport:
    if profile.profile_id not in _SWITCHES:
        if "Core" in profile.name:
            switch = FakeSwitch("core-sw-01", "WS-C3850-24T-L")
            switch.version = "16.12.05b"
        elif profile.connection_type.value == "serial":
            switch = FakeSwitch("sw-access-02", "WS-C2960X-48FPS-L")
        else:
            switch = FakeSwitch()
        _SWITCHES[profile.profile_id] = switch
    return DemoTransport(profile, _SWITCHES[profile.profile_id])


def patch_device_tab() -> None:
    """Make every DeviceTab use the simulator instead of netmiko."""
    from ciscoiosbox.ui import device_tab as device_tab_module

    original_init = device_tab_module.DeviceTab.__init__

    def patched_init(self, profile, parent=None):
        original_init(self, profile, parent)
        self.controller._worker._transport_factory = demo_transport_factory

    device_tab_module.DeviceTab.__init__ = patched_init


def main() -> int:
    screenshots = "--screenshots" in sys.argv

    if screenshots:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from ciscoiosbox.ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("CiscoIOSBox (Demo)")
    apply_theme(app)

    patch_device_tab()

    from ciscoiosbox.ui.main_window import MainWindow

    window = MainWindow(build_store())
    window.setWindowTitle("CiscoIOSBox 0.1.0  —  DEMO (simulated devices)")
    window.resize(1400, 880)
    window.show()

    if screenshots:
        from demo_capture import capture_all

        return capture_all(app, window)

    # Auto-connect the first device so the app opens on something interesting.
    first = window.store.profiles[0]
    window.connect_to(first)
    window.toasts.info(
        "Demo mode: these devices are simulated. Try toggling a port, creating "
        "a VLAN, or typing 'show vlan brief' in the terminal.")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
