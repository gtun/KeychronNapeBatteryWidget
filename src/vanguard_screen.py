"""Draw on the Corsair Vanguard 96 / Vanguard Pro 96 keyboard screen without iCUE.

The screen shows images stored in the keyboard's onboard file system, the same
ones Corsair Web Hub uploads. This module replaces the pixels of one of those
images in place, so nothing new is allocated on the keyboard and no index
files are touched.

Protocol, worked out from Corsair Web Hub's WebHID traffic (Vanguard Pro 96,
1b1c:2b0e, vendor collection usage page 0xFF42 usage 1, 1024-byte reports
with report ID 0). Every packet is 00 01 00 <cmd> ..., replies are 00 00 00 <cmd> <status> ...

    01 <prop u16> <value>      set property (prop 3: 2 = host control, 1 = onboard)
    0d 00 <file u16>           open a file
    09                         size of the open file (u32 at reply[7])
    08                         read next chunk (data at reply[5:])
    06 00 <len u32> <data>     start writing the open file
    07 00 <data>               continue writing
    05 01 00                   close

    file 0x6d60  profile header: tag, u32 Unix time last changed, file IDs; the screen list is the last one
    screen list  37 00 01 <count> <widget file u16>...
    widget       38 00 <image file u16>
    image        48 00 + BMP header cut to 52 bytes + 248x170 RGB565 LE, top row first + 00 00 + u32 Unix time

This is undocumented and reverse-engineered: use at your own risk.
"""

from __future__ import annotations

import struct
import time

from nape_battery import _hid

CORSAIR_VID = 0x1B1C
SCREEN_PIDS = (0x2B0E,)  # Vanguard Pro 96 (wired); other Vanguard 96 PIDs are untested
USAGE_PAGE = 0xFF42
USAGE = 0x0001

WIDTH, HEIGHT = 248, 170
PACKET = 1024
TIMEOUT_MS = 1000

PROP_MODE = 0x0003
MODE_HOST, MODE_ONBOARD = 2, 1

PROFILE_INDEX = 0x000F
TAG_SCREEN_LIST = 0x0037
TAG_WIDGET = 0x0038
TAG_IMAGE = 0x0048

# The first 54 bytes of every image file exactly as Web Hub writes them: the image tag
# and a 16 bpp BMP header. Web Hub cuts the header 2 bytes short, so the pixels start
# at file offset 54, and pads 2 bytes before the timestamp. The keyboard is only
# known to accept this exact layout, so it is copied as is.
IMAGE_HEADER = bytes.fromhex(
    "4800424d9c490100000000003600000028000000f8000000aa000000010010000300000060490100"
    "130b0000130b0000000000000000"
)
IMAGE_SIZE = len(IMAGE_HEADER) + WIDTH * HEIGHT * 2 + 2 + 4


class ScreenError(Exception):
    pass


def find_path(enumerate_fn=None) -> bytes | None:
    if enumerate_fn is None:
        enumerate_fn = _hid().enumerate
    for pid in SCREEN_PIDS:
        for info in enumerate_fn(CORSAIR_VID, pid):
            if info.get("usage_page") == USAGE_PAGE and info.get("usage") == USAGE:
                return info["path"]
    return None


def build_image(rgb565: bytes, timestamp: int | None = None) -> bytes:
    """The keyboard's image file for 248x170 little-endian RGB565 pixels, top row first."""
    if len(rgb565) != WIDTH * HEIGHT * 2:
        raise ValueError(f"expected {WIDTH * HEIGHT * 2} bytes of RGB565, got {len(rgb565)}")
    stamp = int(time.time() if timestamp is None else timestamp)
    return IMAGE_HEADER + rgb565 + b"\x00\x00" + struct.pack("<I", stamp)


class VanguardScreen:
    def __init__(self, device):
        self.device = device

    @classmethod
    def open(cls) -> "VanguardScreen":
        path = find_path()
        if path is None:
            raise ScreenError("Vanguard Pro 96 not found (it must be connected by USB cable)")
        return cls(_hid().Device(path=path))

    def close(self):
        self.device.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transport -------------------------------------------------------------------
    def command(self, cmd: int, payload: bytes = b"") -> bytes:
        packet = bytes((0x00, 0x01, 0x00, cmd)) + payload
        if len(packet) > PACKET:
            raise ValueError("packet too long")
        self.device.write(b"\x00" + packet.ljust(PACKET, b"\x00"))
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while (remaining := deadline - time.monotonic()) > 0:
            reply = bytes(self.device.read(PACKET + 1, max(1, int(remaining * 1000))))
            if len(reply) == PACKET + 1 and reply[0] == 0:  # some stacks keep the report ID
                reply = reply[1:]
            if len(reply) >= 5 and reply[3] == cmd:
                if reply[4] != 0:
                    raise ScreenError(f"command {cmd:02x} failed with status {reply[4]:02x}")
                return reply
        raise ScreenError(f"no reply to command {cmd:02x}")

    def set_mode(self, mode: int):
        self.command(0x01, struct.pack("<HB", PROP_MODE, mode))

    def read_file(self, file_id: int) -> bytes:
        self.command(0x0D, struct.pack("<BH", 0, file_id))
        try:
            size = struct.unpack_from("<I", self.command(0x09), 7)[0]
            data = bytearray()
            while len(data) < size:
                data += self.command(0x08)[5:]
            return bytes(data[:size])
        finally:
            self.command(0x05, b"\x01\x00")

    def write_file(self, file_id: int, data: bytes):
        self.command(0x0D, struct.pack("<BH", 0, file_id))
        try:
            first = PACKET - 9
            self.command(0x06, b"\x00" + struct.pack("<I", len(data)) + data[:first])
            for offset in range(first, len(data), PACKET - 5):
                self.command(0x07, b"\x00" + data[offset : offset + PACKET - 5])
        finally:
            self.command(0x05, b"\x01\x00")

    # -- file system ------------------------------------------------------------------
    def profile_header_id(self) -> int:
        index = self.read_file(PROFILE_INDEX)  # 13 00 05 00 <profile header u16>
        return struct.unpack_from("<H", index, 4)[0]

    def screen_images(self) -> list[int]:
        """Image file IDs on the screen of the first onboard profile, in screen order."""
        header = self.read_file(self.profile_header_id())
        screen_list = self.read_file(struct.unpack_from("<H", header, len(header) - 2)[0])
        tag, _, count = struct.unpack_from("<HBB", screen_list, 0)
        if tag != TAG_SCREEN_LIST:
            raise ScreenError(f"unexpected screen list tag {tag:04x}")
        images = []
        for widget_id in struct.unpack_from(f"<{count}H", screen_list, 4):
            widget = self.read_file(widget_id)
            wtag, image_id = struct.unpack_from("<HH", widget, 0)
            if wtag == TAG_WIDGET:
                images.append(image_id)
        return images

    def file_size(self, file_id: int) -> int:
        self.command(0x0D, struct.pack("<BH", 0, file_id))
        try:
            return struct.unpack_from("<I", self.command(0x09), 7)[0]
        finally:
            self.command(0x05, b"\x01\x00")

    def replace_image(self, image_id: int, rgb565: bytes):
        """Overwrite an existing screen image with new pixels. The screen redraws at once."""
        if self.file_size(image_id) != IMAGE_SIZE:
            raise ScreenError(f"file {image_id:04x} is not a {WIDTH}x{HEIGHT} screen image")
        image = build_image(rgb565)
        self.set_mode(MODE_HOST)
        try:
            self.write_file(image_id, image)
        finally:
            self.set_mode(MODE_ONBOARD)


# Every redraw is a write to the keyboard's flash, so redraw only when the picture
# would change meaningfully, not on every battery read.
REDRAW_STEP = 5
REDRAW_THRESHOLDS = (15, 30)  # colour changes, as in the tray icon


def should_redraw(last: tuple[int, bool] | None, percent: int, charging: bool) -> bool:
    """last is the (percent, charging) currently on the screen, or None if unknown."""
    if last is None:
        return True
    last_percent, last_charging = last
    if charging != last_charging or abs(percent - last_percent) >= REDRAW_STEP:
        return True
    if percent == 100 and last_percent != 100:
        return True
    return any((percent <= t) != (last_percent <= t) for t in REDRAW_THRESHOLDS)


def _test_pattern() -> bytes:
    """Vertical bars (yellow, magenta, cyan, black), unlike Web Hub's test image."""
    colors = (0xFFE0, 0xF81F, 0x07FF, 0x0000)
    row = b"".join(struct.pack("<H", colors[x * 4 // WIDTH]) for x in range(WIDTH))
    return row * HEIGHT


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Corsair Vanguard Pro 96 screen tool (reverse-engineered)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="read-only: list the images on the screen")
    test = sub.add_parser("test", help="replace one screen image with a test pattern")
    test.add_argument("image", help="image file ID in hex, from 'list'")
    args = parser.parse_args()

    with VanguardScreen.open() as screen:
        if args.action == "list":
            header_id = screen.profile_header_id()
            print(f"profile header: {header_id:04x}")
            for image_id in screen.screen_images():
                data = screen.read_file(image_id)
                stamp = struct.unpack_from("<I", data, len(data) - 4)[0] if len(data) >= 4 else 0
                print(f"image {image_id:04x}: {len(data)} bytes, tag {data[:2].hex()}, "
                      f"{data[2:4]!r}, stamp {time.strftime('%Y-%m-%d %H:%M', time.localtime(stamp))}")
        else:
            screen.replace_image(int(args.image, 16), _test_pattern())
            print("written")


if __name__ == "__main__":
    main()
