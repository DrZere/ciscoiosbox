"""Parser tests against captured Cisco output.

These run without Qt, netmiko or a device, so they are the fast feedback loop
for the layer most likely to break against an unfamiliar IOS version.
"""
from __future__ import annotations

import pytest

from ciscoiosbox.core.exceptions import InsufficientPrivilege, InvalidInputError
from ciscoiosbox.parsers.errors import find_ios_error, raise_for_ios_error
from ciscoiosbox.parsers.interfaces import (
    merge_interface_data, normalise_name, parse_interface_descriptions,
    parse_interface_rates, parse_ip_interface_brief, parse_interfaces_status,
    parse_switchport_modes,
)
from ciscoiosbox.parsers.system import (
    build_resource_sample, mask_to_prefix, parse_cpu, parse_default_gateway,
    parse_hostname, parse_interface_address, parse_memory, parse_show_version,
    prefix_to_mask, same_subnet, validate_hostname, validate_ipv4,
    validate_netmask,
)
from ciscoiosbox.parsers.vlans import (
    build_access_port, build_trunk_port, parse_show_vlan_brief,
    parse_vlan_range, validate_vlan_id, validate_vlan_name,
)

# ─── Fixtures: real device output ─────────────────────────────────────────────

IP_BRIEF = """\
Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES NVRAM  administratively down down
Vlan10                 192.168.10.2    YES NVRAM  up                    up
GigabitEthernet1/0/1   unassigned      YES unset  up                    up
GigabitEthernet1/0/2   unassigned      YES unset  down                  down
GigabitEthernet1/0/3   unassigned      YES unset  administratively down down
GigabitEthernet1/0/10  unassigned      YES unset  up                    up
"""

INTERFACES_STATUS = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   uplink to core sw  connected    trunk        full   1000 10/100/1000BaseTX
Gi1/0/2                      notconnect   10           auto   auto 10/100/1000BaseTX
Gi1/0/3   spare port         disabled     10           auto   auto 10/100/1000BaseTX
Gi1/0/10  wifi ap floor 2    connected    20         a-full  a-100 10/100/1000BaseTX
"""

VLAN_BRIEF = """\
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/5, Gi1/0/6, Gi1/0/7
10   Users                            active    Gi1/0/1, Gi1/0/2, Gi1/0/3
                                                Gi1/0/4, Gi1/0/8
20   Voice                            active
1002 fddi-default                     act/unsup
1003 token-ring-default               act/unsup
"""

SHOW_VERSION = """\
Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(4)E7, RELEASE SOFTWARE (fc2)
Technical Support: http://www.cisco.com/techsupport

sw-access-01 uptime is 12 weeks, 3 days, 4 hours, 21 minutes
System returned to ROM by power-on
System image file is "flash:/c2960x-universalk9-mz.152-4.E7.bin"

cisco WS-C2960X-24TS-L (APM86XXX) processor (revision H0) with 131072K bytes of memory.
Processor board ID FOC1234X5YZ
System serial number            : FOC1234X5YZ
Model Number                    : WS-C2960X-24TS-L
"""

SHOW_INTERFACE = """\
GigabitEthernet1/0/1 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00c1.b1a2.0301 (bia 00c1.b1a2.0301)
  Description: uplink to core sw
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
  Full-duplex, 1000Mb/s, media type is 10/100/1000BaseTX
  5 minute input rate 4521000 bits/sec, 812 packets/sec
  5 minute output rate 1203000 bits/sec, 415 packets/sec
     892134521 packets input, 745123698521 bytes, 0 no buffer
     412398765 packets output, 198234567890 bytes, 0 underruns
"""


# ─── Interface name normalisation ─────────────────────────────────────────────

@pytest.mark.parametrize("abbreviated,expected", [
    ("Gi1/0/1", "GigabitEthernet1/0/1"),
    ("gi1/0/1", "GigabitEthernet1/0/1"),
    ("Te1/1/1", "TenGigabitEthernet1/1/1"),
    ("Fa0/1", "FastEthernet0/1"),
    ("Po1", "Port-channel1"),
    ("Vl10", "Vlan10"),
    ("Lo0", "Loopback0"),
    ("GigabitEthernet1/0/1", "GigabitEthernet1/0/1"),   # already canonical
])
def test_normalise_name(abbreviated, expected):
    assert normalise_name(abbreviated) == expected


# ─── show ip interface brief ──────────────────────────────────────────────────

def test_parse_ip_interface_brief():
    rows = parse_ip_interface_brief(IP_BRIEF)
    assert len(rows) == 6

    by_name = {r.name: r for r in rows}
    assert by_name["Vlan10"].ip_address == "192.168.10.2"
    # "unassigned" is a placeholder, not an address.
    assert by_name["GigabitEthernet1/0/1"].ip_address == ""
    assert by_name["Vlan1"].is_shutdown is True
    assert by_name["GigabitEthernet1/0/2"].is_shutdown is False    # down, not shut
    assert by_name["GigabitEthernet1/0/3"].is_shutdown is True


# ─── show interfaces status ───────────────────────────────────────────────────

def test_parse_interfaces_status_preserves_spaced_descriptions():
    """Descriptions contain spaces; column-offset parsing must keep them whole."""
    rows = parse_interfaces_status(INTERFACES_STATUS)
    by_name = {r.name: r for r in rows}

    assert by_name["GigabitEthernet1/0/1"].description == "uplink to core sw"
    assert by_name["GigabitEthernet1/0/10"].description == "wifi ap floor 2"
    assert by_name["GigabitEthernet1/0/2"].description == ""        # blank column
    assert by_name["GigabitEthernet1/0/1"].vlan == "trunk"
    assert by_name["GigabitEthernet1/0/10"].duplex == "a-full"


def test_parse_interfaces_status_empty_on_router():
    """A router has no such command; the parser must return [] not raise."""
    assert parse_interfaces_status("% Invalid input detected at '^' marker.") == []


# ─── merge ────────────────────────────────────────────────────────────────────

def test_merge_interface_data():
    merged = merge_interface_data(
        parse_ip_interface_brief(IP_BRIEF),
        parse_interfaces_status(INTERFACES_STATUS),
    )
    by_name = {r.name: r for r in merged}
    assert len(merged) == 6

    uplink = by_name["GigabitEthernet1/0/1"]
    assert uplink.description == "uplink to core sw"     # from status
    assert uplink.oper_status == "connected"             # status wins over ip-brief
    assert uplink.mode == "trunk"
    assert uplink.is_shutdown is False

    # "disabled" in the status column implies administratively down.
    spare = by_name["GigabitEthernet1/0/3"]
    assert spare.is_shutdown is True

    # An L3 SVI appears only in ip-brief and must survive the merge.
    assert by_name["Vlan10"].ip_address == "192.168.10.2"


def test_merge_sorts_naturally():
    """Gi1/0/2 must sort before Gi1/0/10, not lexicographically after it."""
    merged = merge_interface_data(parse_ip_interface_brief(IP_BRIEF))
    gig_names = [r.name for r in merged if r.name.startswith("Gigabit")]
    assert gig_names == [
        "GigabitEthernet1/0/1", "GigabitEthernet1/0/2",
        "GigabitEthernet1/0/3", "GigabitEthernet1/0/10",
    ]


def test_parse_switchport_modes():
    output = """\
Name: Gi1/0/1
Switchport: Enabled
Administrative Mode: trunk
Operational Mode: trunk

Name: Gi1/0/2
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
"""
    modes = parse_switchport_modes(output)
    assert modes["GigabitEthernet1/0/1"] == "trunk"
    assert modes["GigabitEthernet1/0/2"] == "access"


def test_parse_interface_descriptions():
    output = """\
Interface                      Status         Protocol Description
Gi1/0/1                        up             up       uplink to core sw
Gi1/0/2                        admin down     down
"""
    descriptions = parse_interface_descriptions(output)
    assert descriptions["GigabitEthernet1/0/1"] == "uplink to core sw"
    assert descriptions["GigabitEthernet1/0/2"] == ""


# ─── VLANs ────────────────────────────────────────────────────────────────────

def test_parse_vlan_brief_handles_wrapped_port_lists():
    vlans = parse_show_vlan_brief(VLAN_BRIEF)
    by_id = {v.vlan_id: v for v in vlans}

    assert set(by_id) == {1, 10, 20, 1002, 1003}
    # VLAN 10's ports wrap onto a continuation line — all 5 must be captured.
    assert len(by_id[10].interfaces) == 5
    assert "GigabitEthernet1/0/8" in by_id[10].interfaces
    assert by_id[20].interfaces == []
    assert by_id[1].is_default and by_id[1002].is_default
    assert not by_id[10].is_default


@pytest.mark.parametrize("text,expected", [
    ("10", [10]),
    ("10,20,30", [10, 20, 30]),
    ("30-33", [30, 31, 32, 33]),
    ("10,20,30-32", [10, 20, 30, 31, 32]),
    ("", []),
])
def test_parse_vlan_range_valid(text, expected):
    ids, error = parse_vlan_range(text)
    assert error == ""
    assert ids == expected


@pytest.mark.parametrize("text", ["abc", "10,abc", "30-20", "10,,x", "5000"])
def test_parse_vlan_range_invalid(text):
    ids, error = parse_vlan_range(text)
    assert error != ""
    assert ids == []


@pytest.mark.parametrize("vlan_id,valid", [
    (1, True), (10, True), (4094, True),
    (0, False), (4095, False), (1002, False), (1005, False),
])
def test_validate_vlan_id(vlan_id, valid):
    assert (validate_vlan_id(vlan_id) == "") is valid


@pytest.mark.parametrize("name,valid", [
    ("Users", True), ("VOICE_10", True), ("", True),          # blank is optional
    ("has space", False), ("x" * 33, False), ("bad!char", False),
])
def test_validate_vlan_name(name, valid):
    assert (validate_vlan_name(name) == "") is valid


def test_build_access_port():
    commands = build_access_port("Gi1/0/5", 10, voice_vlan=20)
    assert commands == [
        "interface Gi1/0/5", "switchport mode access",
        "switchport access vlan 10", "switchport voice vlan 20", "exit",
    ]


def test_build_trunk_port():
    commands = build_trunk_port("Gi1/0/1", allowed="10,20", native_vlan=99)
    assert "switchport trunk encapsulation dot1q" in commands
    assert "switchport mode trunk" in commands
    assert "switchport trunk allowed vlan 10,20" in commands
    assert "switchport trunk native vlan 99" in commands


# ─── show version ─────────────────────────────────────────────────────────────

def test_parse_show_version():
    info = parse_show_version(SHOW_VERSION)
    assert info.hostname == "sw-access-01"
    assert info.model == "WS-C2960X-24TS-L"
    assert info.version == "15.2(4)E7"
    assert info.serial_number == "FOC1234X5YZ"
    assert "12 weeks" in info.uptime
    assert info.image.endswith(".bin")


# ─── CPU & memory ─────────────────────────────────────────────────────────────

def test_parse_cpu_ios():
    output = ("CPU utilization for five seconds: 7%/0%; one minute: 8%; "
              "five minutes: 9%")
    assert parse_cpu(output) == (7.0, 8.0, 9.0)


def test_parse_cpu_nxos():
    five, one, _ = parse_cpu("CPU util  :   3.5% user,   1.0% kernel,  95.5% idle")
    assert five == pytest.approx(4.5)
    assert one == pytest.approx(4.5)


def test_parse_cpu_unrecognised_returns_zeros():
    """An unparseable response must degrade to a flat line, not an exception."""
    assert parse_cpu("something entirely different") == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("output,used,free", [
    ("Processor Pool Total: 212933812 Used:  74253464 Free: 138680348",
     74253464, 138680348),
    ("Memory usage:   8151656K total,   3129404K used,   5022252K free",
     3129404 * 1024, 5022252 * 1024),
])
def test_parse_memory(output, used, free):
    assert parse_memory(output) == (used, free)


def test_parse_memory_statistics_table():
    output = """\
                Head    Total(b)     Used(b)     Free(b)   Lowest(b)  Largest(b)
Processor   2A3B4C5D   212933812    74253464   138680348   130000000  120000000
      I/O   3B4C5D6E    35651584     8000000    27651584    27000000   26000000
"""
    assert parse_memory(output) == (74253464, 138680348)


def test_build_resource_sample_computes_percentage():
    sample = build_resource_sample(
        "CPU utilization for five seconds: 20%/0%; one minute: 25%; five minutes: 30%",
        "Processor Pool Total: 200 Used: 50 Free: 150",
        timestamp=1000.0)
    assert sample.cpu_5sec == 20.0
    assert sample.mem_total_bytes == 200
    assert sample.mem_used_percent == pytest.approx(25.0)


# ─── Interface rates ──────────────────────────────────────────────────────────

def test_parse_interface_rates():
    data = parse_interface_rates(SHOW_INTERFACE)
    assert data["rx_bps"] == 4521000.0
    assert data["tx_bps"] == 1203000.0
    assert data["rx_pps"] == 812.0
    assert data["bandwidth_bps"] == 1e9              # BW 1000000 Kbit/sec
    assert data["rx_octets"] == 745123698521


# ─── Config fragment parsing ──────────────────────────────────────────────────

def test_parse_hostname_and_gateway():
    assert parse_hostname("hostname sw-access-01\n") == "sw-access-01"
    assert parse_default_gateway("ip default-gateway 192.168.10.1") == "192.168.10.1"
    assert parse_default_gateway(
        "ip route 0.0.0.0 0.0.0.0 10.0.0.1") == "10.0.0.1"


def test_parse_interface_address():
    assert parse_interface_address(
        "interface Vlan10\n ip address 192.168.10.2 255.255.255.0\n") == (
        "192.168.10.2", "255.255.255.0")
    assert parse_interface_address("interface Vlan10\n ip address dhcp\n") == (
        "dhcp", "")


# ─── Validation helpers ───────────────────────────────────────────────────────

@pytest.mark.parametrize("hostname,valid", [
    ("switch-01", True), ("SW1", True),
    ("", False), ("1switch", False), ("has space", False),
    ("ends-", False), ("x" * 64, False),
])
def test_validate_hostname(hostname, valid):
    assert (validate_hostname(hostname) == "") is valid


@pytest.mark.parametrize("address,valid", [
    ("192.168.1.1", True), ("0.0.0.0", True), ("255.255.255.255", True),
    ("256.1.1.1", False), ("1.2.3", False), ("192.168.01.1", False),
    ("not.an.ip.addr", False),
])
def test_validate_ipv4(address, valid):
    assert (validate_ipv4(address) == "") is valid


@pytest.mark.parametrize("mask,valid", [
    ("255.255.255.0", True), ("255.255.0.0", True), ("255.255.255.252", True),
    ("255.255.0.255", False),      # non-contiguous
    ("255.255.255.1", False),
])
def test_validate_netmask(mask, valid):
    assert (validate_netmask(mask) == "") is valid


def test_mask_prefix_roundtrip():
    for prefix in (8, 16, 24, 30, 32):
        assert mask_to_prefix(prefix_to_mask(prefix)) == prefix


def test_same_subnet():
    assert same_subnet("192.168.10.2", "192.168.10.1", "255.255.255.0")
    assert not same_subnet("192.168.10.2", "10.0.0.1", "255.255.255.0")


# ─── IOS error detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("output,exception", [
    ("% Invalid input detected at '^' marker.", InvalidInputError),
    ("% Incomplete command.", InvalidInputError),
    ("% Ambiguous command:  \"sh in\"", InvalidInputError),
    ("Command authorization failed.", InsufficientPrivilege),
])
def test_raise_for_ios_error(output, exception):
    with pytest.raises(exception):
        raise_for_ios_error("show foo", output)


@pytest.mark.parametrize("output", [
    "GigabitEthernet1/0/1 is up, line protocol is up",
    "% Warning: portfast should only be enabled on ports connected to a single host",
    "Building configuration...\n[OK]",
    "",
])
def test_benign_output_is_not_an_error(output):
    """Informational %-lines must not be mistaken for failures."""
    assert find_ios_error(output) is None
    raise_for_ios_error("show run", output)      # must not raise


def test_error_message_includes_device_line():
    error = find_ios_error("% Invalid input detected at '^' marker.")
    exc = error.as_exception(command="show fooo")
    assert "Invalid input" in exc.user_message
    assert exc.command == "show fooo"


# ─── Sort keys ────────────────────────────────────────────────────────────────

def test_natural_sort_key_orders_numerically():
    """Regression: a stringified tuple sorted Gi1/0/10 before Gi1/0/2.

    Qt compares whatever the sort role returns, so the key must be a string that
    already sorts correctly under plain string comparison.
    """
    from ciscoiosbox.parsers.interfaces import natural_sort_key

    names = [
        "GigabitEthernet1/0/1", "GigabitEthernet1/0/10",
        "GigabitEthernet1/0/2", "GigabitEthernet1/0/12",
        "GigabitEthernet1/0/9", "TenGigabitEthernet1/1/1", "Vlan10", "Vlan2",
    ]
    assert sorted(names, key=natural_sort_key) == [
        "GigabitEthernet1/0/1", "GigabitEthernet1/0/2",
        "GigabitEthernet1/0/9", "GigabitEthernet1/0/10",
        "GigabitEthernet1/0/12", "TenGigabitEthernet1/1/1",
        "Vlan2", "Vlan10",
    ]


def test_natural_sort_key_is_a_string():
    """The key must be directly string-comparable, not a repr of a tuple."""
    from ciscoiosbox.parsers.interfaces import natural_sort_key

    key = natural_sort_key("GigabitEthernet1/0/2")
    assert isinstance(key, str)
    assert key < natural_sort_key("GigabitEthernet1/0/10")
