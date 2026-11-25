import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import vanguard_screen as vs  # noqa: E402

PIXELS = vs.WIDTH * vs.HEIGHT * 2


class FakeKeyboard:
    """Answers the screen protocol from an in-memory file system."""

    def __init__(self, files):
        self.files = dict(files)
        self.replies = []
        self.sent = []
        self.mode = vs.MODE_ONBOARD
        self.open_file = None

    def write(self, report):
        assert report[0] == 0 and len(report) == vs.PACKET + 1
        packet = report[1:]
        self.sent.append(packet)
        cmd = packet[3]
        reply = bytearray(vs.PACKET)
        reply[3] = cmd
        if cmd == 0x01:
            self.mode = packet[6]
        elif cmd == 0x0D:
            self.open_file, self.pos = struct.unpack_from("<H", packet, 5)[0], 0
        elif cmd == 0x09:
            struct.pack_into("<I", reply, 7, len(self.files[self.open_file]))
        elif cmd == 0x08:
            chunk = self.files[self.open_file][self.pos:self.pos + vs.PACKET - 5]
            reply[5:5 + len(chunk)] = chunk
            self.pos += vs.PACKET - 5
        elif cmd == 0x06:
            assert self.mode == vs.MODE_HOST
            self.length = struct.unpack_from("<I", packet, 5)[0]
            self.buffer = bytearray(packet[9:])
        elif cmd == 0x07:
            self.buffer += packet[5:]
        elif cmd == 0x05 and getattr(self, "buffer", None) is not None:
            self.files[self.open_file] = bytes(self.buffer[:self.length])
            self.buffer = None
        self.replies.append(bytes(reply))

    def read(self, size, timeout):
        return self.replies.pop(0)

    def close(self):
        pass


def keyboard():
    return FakeKeyboard({
        0x000F: bytes.fromhex("13000500606d"),
        0x6D60: bytes.fromhex("1ef1a45c066b636d696d00002100") + b"VANGUARD PRO 96 Default Profile 1" + bytes.fromhex("006d9e6d"),
        0x6D9E: bytes.fromhex("370001029c6da06d"),
        0x6D9C: bytes.fromhex("39001234"),  # not an image widget
        0x6DA0: bytes.fromhex("38009f6d") + bytes(12),
        0x6D9F: vs.build_image(bytes(PIXELS), 0),
    })


def test_image_layout_matches_web_hub():
    image = vs.build_image(b"\x00\xf8" * (PIXELS // 2), 0x6B065CA1)
    assert len(image) == vs.IMAGE_SIZE == 84380
    assert image[:6].hex() == "4800424d9c49"
    assert image[54:56] == b"\x00\xf8"          # first pixel, right after the cut header
    assert image[-6:].hex() == "0000a15c066b"   # padding, then the Unix time


def test_build_image_rejects_wrong_size():
    with pytest.raises(ValueError):
        vs.build_image(b"\x00" * 10)


def test_screen_images_follows_profile_to_image_files():
    assert vs.VanguardScreen(keyboard()).screen_images() == [0x6D9F]


def test_replace_image_writes_pixels_in_host_mode():
    kb = keyboard()
    pixels = bytes(range(256)) * (PIXELS // 256) + bytes(PIXELS % 256)
    vs.VanguardScreen(kb).replace_image(0x6D9F, pixels)
    written = kb.files[0x6D9F]
    assert written[54:54 + PIXELS] == pixels
    assert kb.mode == vs.MODE_ONBOARD
    assert all(len(p) == vs.PACKET for p in kb.sent)


def test_replace_image_refuses_other_files():
    with pytest.raises(vs.ScreenError):
        vs.VanguardScreen(keyboard()).replace_image(0x6DA0, bytes(PIXELS))


def test_failed_status_raises():
    kb = keyboard()
    original = kb.write

    def failing(report):
        original(report)
        reply = bytearray(kb.replies.pop())
        reply[4] = 0x03
        kb.replies.append(bytes(reply))

    kb.write = failing
    with pytest.raises(vs.ScreenError):
        vs.VanguardScreen(kb).file_size(0x6D9F)


@pytest.mark.parametrize("last, percent, charging, expected", [
    (None, 50, False, True),
    ((50, False), 50, False, False),
    ((50, False), 47, False, False),
    ((50, False), 45, False, True),       # 5% step
    ((50, False), 50, True, True),        # charging changed
    ((32, False), 30, False, True),       # amber threshold
    ((16, False), 15, False, True),       # red threshold
    ((97, True), 100, True, True),        # full
    ((99, True), 100, False, True),
])
def test_should_redraw(last, percent, charging, expected):
    assert vs.should_redraw(last, percent, charging) is expected
