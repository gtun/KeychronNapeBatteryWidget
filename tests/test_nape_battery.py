import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import nape_battery as nb  # noqa: E402


def packet(*data):
    return bytes(data).ljust(32, b"\x00")


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def read(self, size, timeout):
        return self.replies.pop(0) if self.replies else b""


def test_request_skips_unsolicited_reports():
    device = FakeDevice([packet(0xA3, 0x02, 0x06), packet(0xBC, 0x01), packet(0xA7, 0x31, 87, 0)])
    reply = nb._request(device, bytes((0xA7, 0x31)), timeout_ms=200)
    assert nb.parse_battery(reply) == (87, False)
    assert device.written == [b"\x00" + packet(0xA7, 0x31)]


def test_request_times_out():
    assert nb._request(FakeDevice([]), bytes((0xA7, 0x31)), timeout_ms=20) is None


def test_parse_battery_charging():
    assert nb.parse_battery(packet(0xA7, 0x31, 100, 1)) == (100, True)


def test_parse_battery_rejects_garbage():
    with pytest.raises(ValueError):
        nb.parse_battery(packet(0xA7, 0x31, 200, 0))
    with pytest.raises(ValueError):
        nb.parse_battery(packet(0xA7, 0x20, 50, 0))


def receiver_state(nape_status):
    # three slots at offsets 2, 7, 12: VID BE16, PID BE16, status
    data = bytearray(packet(0xB2))
    data[2:7] = bytes((0x34, 0x34, 0x02, 0x50, 1))  # some keyboard
    data[7:12] = bytes((0x34, 0x34, 0x40, 0x04, nape_status))
    return bytes(data)


def test_receiver_slot_detection():
    assert nb._nape_paired_and_awake(FakeDevice([receiver_state(1)]), 50) is True
    assert nb._nape_paired_and_awake(FakeDevice([receiver_state(0)]), 50) is False
    assert nb._nape_paired_and_awake(FakeDevice([]), 20) is None


def test_find_interfaces_prefers_cable_and_filters_collections():
    devices = {
        nb.NAPE_USB_PID: [
            {"usage_page": 0x0001, "usage": 0x02, "path": b"mouse"},
            {"usage_page": 0xFF60, "usage": 0x61, "path": b"usb-raw"},
        ],
        nb.LINK_KM_PID: [
            {"usage_page": 0x008C, "usage": 0x01, "path": b"bridge"},
            {"usage_page": 0xFF60, "usage": 0x61, "path": b"rx-raw"},
        ],
    }
    found = nb.find_interfaces(lambda vid, pid: devices.get(pid, []))
    assert found == [(nb.Connection.WIRED, b"usb-raw"), (nb.Connection.WIRELESS, b"rx-raw")]
