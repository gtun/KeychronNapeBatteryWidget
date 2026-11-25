import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nape_battery import BatteryReading, Connection, Status  # noqa: E402
from nape_bridge import BatteryBridge, snapshot  # noqa: E402


def test_snapshot_ok():
    reading = BatteryReading(Status.OK, Connection.WIRELESS, 87, True)
    data = snapshot(reading, (reading, 100.0), 100.0)
    assert data["status"] == "ok"
    assert data["percent"] == 87
    assert data["charging"] is True
    assert data["connection"] == "wireless"
    assert data["last_percent"] == 87


def test_snapshot_asleep_keeps_last_reading():
    last = BatteryReading(Status.OK, Connection.WIRED, 42)
    data = snapshot(BatteryReading(Status.ASLEEP, Connection.WIRELESS), (last, 50.0), 99.0)
    assert data["status"] == "asleep"
    assert data["percent"] is None
    assert data["last_percent"] == 42
    assert data["last_updated"] == 50.0
    assert data["message"]


def test_snapshot_before_first_read():
    assert snapshot(None, None, None)["status"] == "starting"


@pytest.fixture
def bridge():
    b = BatteryBridge(port=0)
    b.start()
    yield b
    b.stop()


def _get(bridge, path="/battery", host=None):
    request = urllib.request.Request(f"http://127.0.0.1:{bridge.port}{path}")
    if host:
        request.add_header("Host", host)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.headers, json.loads(response.read())


def test_serves_published_reading(bridge):
    bridge.publish(BatteryReading(Status.OK, Connection.WIRELESS, 63), None, 10.0)
    headers, data = _get(bridge)
    assert data["percent"] == 63
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cache-Control"] == "no-store"


def test_localhost_host_header_allowed(bridge):
    _, data = _get(bridge, host=f"localhost:{bridge.port}")
    assert data["status"] == "starting"


def test_rejects_foreign_host_header(bridge):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(bridge, host="evil.example:80")
    assert exc.value.code == 403


def test_unknown_path_is_404(bridge):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(bridge, path="/other")
    assert exc.value.code == 404


def test_restart_after_stop():
    b = BatteryBridge(port=0)
    b.start()
    b.stop()
    assert not b.running
    b.start()
    assert b.running
    b.stop()
