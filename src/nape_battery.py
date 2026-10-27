"""Battery query for the Keychron Nape Pro trackball.

The Nape Pro (ZMK firmware) exposes a VIA-style Raw HID collection
(usage page 0xFF60, usage 0x61) that takes unnumbered 32-byte packets.
The same collection exists on the Link-KM 2.4 GHz receiver, which tunnels
mouse commands to the paired Nape.

    request : A7 31 00 .. 00          (32 bytes, plus a leading 0x00 report ID)
    reply   : A7 31 <percent> <charging> ..

Protocol details come from the reverse-engineering work in
https://github.com/nvalis/nape_pro and https://github.com/ky0209/NapeBar.

This module has no Qt dependency so it can be used and tested on its own:
    python3 nape_battery.py
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from enum import Enum

KEYCHRON_VID = 0x3434
NAPE_USB_PID = 0x0440      # Nape Pro connected by USB cable
LINK_KM_PID = 0xD026       # Keychron Link-KM 2.4 GHz receiver
NAPE_WIRELESS_PID = 0x4004 # PID the receiver reports for a paired Nape

RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61

REPORT_SIZE = 32
MISC_CMD = 0xA7
GET_BATTERY = 0x31
RECEIVER_STATE = 0xB2


class Connection(Enum):
    WIRED = "wired"
    WIRELESS = "wireless"


class Status(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"        # no receiver and no cable connection
    ASLEEP = "asleep"              # receiver present, Nape not connected/answering
    NO_PERMISSION = "no_permission"
    ERROR = "error"


@dataclass
class BatteryReading:
    status: Status
    connection: Connection | None = None
    percent: int | None = None
    charging: bool = False
    detail: str = ""


_hid_module = None


def _hid():
    # Imported lazily so the module stays importable (and testable) without hidapi.
    global _hid_module
    if _hid_module is not None:
        return _hid_module
    if sys.platform == "win32":
        # Python 3.8+ no longer searches PATH for ctypes DLLs; look for hidapi.dll
        # inside a PyInstaller bundle, next to the exe, or next to this script.
        # Only register it once: every add_dll_directory call adds a permanent
        # entry, and repeating it on each poll eventually fails with WinError 206.
        for folder in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable),
                       os.path.dirname(os.path.abspath(__file__))):
            if folder and os.path.exists(os.path.join(folder, "hidapi.dll")):
                os.add_dll_directory(folder)
                break
    import hid

    _hid_module = hid
    return hid


def find_interfaces(enumerate_fn=None) -> list[tuple[Connection, bytes]]:
    """Return Raw HID paths for a cabled Nape first, then the Link-KM receiver."""
    if enumerate_fn is None:
        enumerate_fn = _hid().enumerate
    found: list[tuple[Connection, bytes]] = []
    for pid, connection in ((NAPE_USB_PID, Connection.WIRED), (LINK_KM_PID, Connection.WIRELESS)):
        for info in enumerate_fn(KEYCHRON_VID, pid):
            if info.get("usage_page") == RAW_USAGE_PAGE and info.get("usage") == RAW_USAGE:
                found.append((connection, info["path"]))
    return found


def _request(device, payload: bytes, timeout_ms: int) -> bytes | None:
    """Send one request and wait for the reply that echoes its prefix.

    The channel has no transaction IDs and also carries unsolicited reports
    (layer changes, receiver notifications), so anything else is skipped.
    """
    device.write(b"\x00" + payload.ljust(REPORT_SIZE, b"\x00"))
    deadline = time.monotonic() + timeout_ms / 1000
    while (remaining := deadline - time.monotonic()) > 0:
        reply = bytes(device.read(64, max(1, int(remaining * 1000))))
        if reply.startswith(payload):
            return reply
    return None


def _nape_paired_and_awake(device, timeout_ms: int) -> bool | None:
    """Ask the receiver whether a Nape slot is connected; None if unknown."""
    state = _request(device, bytes((RECEIVER_STATE,)), timeout_ms)
    if state is None or len(state) < 17:
        return None
    for offset in (2, 7, 12):
        pid = int.from_bytes(state[offset + 2 : offset + 4], "big")
        if pid == NAPE_WIRELESS_PID:
            return state[offset + 4] == 1
    return None


def parse_battery(reply: bytes) -> tuple[int, bool]:
    if len(reply) < 4 or reply[0] != MISC_CMD or reply[1] != GET_BATTERY:
        raise ValueError(f"unexpected battery reply: {reply[:8].hex(' ')}")
    if reply[2] > 100:
        raise ValueError(f"invalid battery percentage {reply[2]}")
    return reply[2], bool(reply[3])


def read_battery(timeout_ms: int = 1500) -> BatteryReading:
    try:
        hid = _hid()
        interfaces = find_interfaces()
    except Exception as exc:  # hidapi library missing or broken
        return BatteryReading(Status.ERROR, detail=str(exc))
    if not interfaces:
        return BatteryReading(Status.NOT_FOUND)

    last = BatteryReading(Status.ERROR)
    for connection, path in interfaces:
        try:
            with hid.Device(path=path) as device:
                if connection is Connection.WIRELESS:
                    if _nape_paired_and_awake(device, timeout_ms) is False:
                        last = BatteryReading(Status.ASLEEP, connection)
                        continue
                reply = _request(device, bytes((MISC_CMD, GET_BATTERY)), timeout_ms)
                if reply is None:
                    last = BatteryReading(Status.ASLEEP, connection)
                    continue
                percent, charging = parse_battery(reply)
                return BatteryReading(Status.OK, connection, percent, charging)
        except hid.HIDException as exc:
            status = Status.NO_PERMISSION if "ermission" in str(exc) or "open" in str(exc) else Status.ERROR
            last = BatteryReading(status, connection, detail=str(exc))
        except ValueError as exc:
            last = BatteryReading(Status.ERROR, connection, detail=str(exc))
    return last


if __name__ == "__main__":
    print(read_battery())
