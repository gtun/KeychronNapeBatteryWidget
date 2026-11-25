"""Local HTTP bridge that feeds the Nape Pro battery level to the iCUE widget
on the Corsair Vanguard 96 / Vanguard Pro 96 keyboard screen.

iCUE widgets are HTML pages running inside iCUE's embedded Chromium. They
cannot open HID devices, but they may fetch() from localhost on a port that
the widget's manifest declares. This module serves the last battery reading as
JSON on http://localhost:47815/battery, and nothing else.

The server binds to 127.0.0.1 only, answers GET requests only, and rejects
requests whose Host header is not a loopback name (a DNS-rebinding guard).

It has no Qt dependency. The tray widget (nape_widget.py) can host it, or it
can run on its own without a tray icon:
    python3 nape_bridge.py [--interval 300] [--port 47815]
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nape_battery import BatteryReading, Status

BRIDGE_PORT = 47815  # must match the "url" permission in the widget's manifest.json
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

MESSAGES = {
    Status.OK: "",
    Status.NOT_FOUND: "Nape Pro not detected",
    Status.ASLEEP: "Asleep or out of range",
    Status.NO_PERMISSION: "No permission to open the device",
    Status.ERROR: "Could not read battery",
}


def snapshot(reading: BatteryReading | None, last_ok: tuple[BatteryReading, float] | None,
             updated: float | None) -> dict:
    """The JSON document the widget reads. Times are Unix seconds."""
    if reading is None:
        return {"status": "starting", "message": "Reading...", "updated": None,
                "last_percent": None, "last_updated": None}
    data = {
        "status": reading.status.value,
        "message": MESSAGES[reading.status],
        "percent": reading.percent,
        "charging": reading.charging,
        "connection": reading.connection.value if reading.connection else None,
        "updated": updated,
        "last_percent": None,
        "last_updated": None,
    }
    if last_ok is not None:
        data["last_percent"] = last_ok[0].percent
        data["last_updated"] = last_ok[1]
    return data


class BatteryBridge:
    """Holds the latest reading and serves it over HTTP from a background thread."""

    def __init__(self, port: int = BRIDGE_PORT, host: str = "127.0.0.1"):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._body = json.dumps(snapshot(None, None, None)).encode()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def publish(self, reading: BatteryReading, last_ok: tuple[BatteryReading, float] | None,
                updated: float | None = None):
        body = json.dumps(snapshot(reading, last_ok, time.time() if updated is None else updated)).encode()
        with self._lock:
            self._body = body

    def body(self) -> bytes:
        with self._lock:
            return self._body

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self):
        """Start serving. Raises OSError if the port is taken."""
        if self._server is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
                if host not in LOOPBACK_HOSTS:
                    self.send_error(403)
                    return
                if self.path.split("?", 1)[0] != "/battery":
                    self.send_error(404)
                    return
                body = bridge.body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # iCUE widgets load from a local file, so the page has no web
                # origin. The data is only a battery percentage.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # stay quiet; this runs windowless
                pass

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]  # resolves port 0 in tests
        self._thread = threading.Thread(target=self._server.serve_forever, name="nape-bridge", daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None


def main():
    from nape_battery import read_battery

    parser = argparse.ArgumentParser(description="Serve the Nape Pro battery level to the iCUE Vanguard widget")
    parser.add_argument("--interval", type=int, default=300, help="seconds between battery reads (default: 300)")
    parser.add_argument("--port", type=int, default=BRIDGE_PORT,
                        help=f"port to listen on (default: {BRIDGE_PORT}; the widget expects {BRIDGE_PORT})")
    args = parser.parse_args()

    bridge = BatteryBridge(args.port)
    bridge.start()
    print(f"Serving http://localhost:{bridge.port}/battery (Ctrl+C to stop)")
    last_ok = None
    try:
        while True:
            reading = read_battery()
            if reading.status is Status.OK:
                last_ok = (reading, time.time())
            bridge.publish(reading, last_ok)
            print(time.strftime("%H:%M:%S"), reading)
            time.sleep(max(args.interval, 10))
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
