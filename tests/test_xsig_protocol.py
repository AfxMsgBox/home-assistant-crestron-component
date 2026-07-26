"""Unit tests for the pure XSIG codec (xsig_protocol.py).

No TCP server, no asyncio — just bytes in / frames out and back. Covers the
edge cases that are awkward to exercise through the socket: frames split across
feeds, multiple frames in one feed, invalid UTF-8, unknown frames, and the
serial-terminator scan limit.
"""

import unittest

from loader import load

p = load("xsig_protocol")


# Independent encoders that mirror the wire format (don't reuse the impl), so a
# bug in encode_* can't hide behind a matching bug in the test's expectation.
def ref_digital(join, value):
    flag = 0b00100000 if not value else 0
    return bytes([0b10000000 | flag | ((join - 1) >> 7), (join - 1) & 0x7F])


def ref_analog(join, value):
    return bytes([
        0b11000000 | ((value >> 10) & 0b00110000) | ((join - 1) >> 7),
        (join - 1) & 0x7F,
        (value >> 7) & 0x7F,
        value & 0x7F,
    ])


def ref_serial(join, text):
    body = text.encode("utf-8")
    return bytes([0b11001000 | ((join - 1) >> 7), (join - 1) & 0x7F]) + body + b"\xff"


class EncodeTests(unittest.TestCase):
    def test_digital_matches_reference(self):
        for join in (1, 5, 128, 4096):
            for v in (0, 1):
                self.assertEqual(p.encode_digital(join, v), ref_digital(join, v))

    def test_analog_matches_reference_and_clamps(self):
        self.assertEqual(p.encode_analog(3, 1000), ref_analog(3, 1000))
        self.assertEqual(p.encode_analog(1, 65535), ref_analog(1, 65535))
        # Over/under range clamps to 0..65535.
        self.assertEqual(p.encode_analog(2, 70000), ref_analog(2, 65535))
        self.assertEqual(p.encode_analog(2, -5), ref_analog(2, 0))

    def test_serial_matches_reference(self):
        self.assertEqual(
            p.encode_serial(6, "hi".encode("utf-8")), ref_serial(6, "hi")
        )
        self.assertEqual(
            p.encode_serial(4, "你好".encode("utf-8")), ref_serial(4, "你好")
        )


class DecodeRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.d = p.FrameDecoder()

    def test_digital(self):
        frames = self.d.feed(ref_digital(5, 1))
        self.assertEqual(frames, [p.Frame("digital", 5, 1)])
        frames = self.d.feed(ref_digital(5, 0))
        self.assertEqual(frames, [p.Frame("digital", 5, 0)])

    def test_analog(self):
        frames = self.d.feed(ref_analog(3, 12345))
        self.assertEqual(frames, [p.Frame("analog", 3, 12345)])

    def test_analog_full_scale(self):
        frames = self.d.feed(ref_analog(1, 65535))
        self.assertEqual(frames, [p.Frame("analog", 1, 65535)])

    def test_serial_utf8(self):
        frames = self.d.feed(ref_serial(4, "你好 hi"))
        self.assertEqual(frames, [p.Frame("serial", 4, "你好 hi")])

    def test_sync_all_control_byte(self):
        frames = self.d.feed(b"\xfb")
        self.assertEqual(frames, [p.Frame("sync_all", None, None)])


class DecodeFramingTests(unittest.TestCase):
    def setUp(self):
        self.d = p.FrameDecoder()

    def test_frame_split_one_byte_at_a_time(self):
        out = []
        for b in ref_analog(7, 12345):
            out.extend(self.d.feed(bytes([b])))
        self.assertEqual(out, [p.Frame("analog", 7, 12345)])

    def test_multiple_frames_in_one_feed(self):
        blob = ref_digital(5, 1) + ref_analog(3, 99) + ref_serial(4, "x")
        frames = self.d.feed(blob)
        self.assertEqual(
            frames,
            [
                p.Frame("digital", 5, 1),
                p.Frame("analog", 3, 99),
                p.Frame("serial", 4, "x"),
            ],
        )

    def test_partial_then_rest(self):
        blob = ref_analog(3, 99)
        self.assertEqual(self.d.feed(blob[:2]), [])  # incomplete
        self.assertEqual(self.d.feed(blob[2:]), [p.Frame("analog", 3, 99)])

    def test_serial_split_around_terminator(self):
        blob = ref_serial(4, "hello")
        self.assertEqual(self.d.feed(blob[:-1]), [])  # 0xFF not yet arrived
        self.assertEqual(self.d.feed(blob[-1:]), [p.Frame("serial", 4, "hello")])


class DecodeEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.d = p.FrameDecoder()

    def test_invalid_utf8_serial(self):
        # 0xC3 0x28 is an invalid 2-byte UTF-8 sequence.
        header = bytes([0b11001000 | (0 >> 7), 0 & 0x7F])  # join 1
        frame_bytes = header + b"\xc3\x28" + b"\xff"
        frames = self.d.feed(frame_bytes)
        self.assertEqual(frames, [p.Frame("bad_utf8", 1, None)])
        # Decoder recovers: a following valid frame still parses.
        self.assertEqual(
            self.d.feed(ref_digital(2, 1)), [p.Frame("digital", 2, 1)]
        )

    def test_unknown_frame_consumes_two_bytes(self):
        # 0x00 0x00 matches no known frame mask.
        frames = self.d.feed(b"\x00\x00" + ref_digital(5, 1))
        self.assertEqual(frames[0].kind, "unknown")
        self.assertEqual(frames[0].value, "0000")
        self.assertEqual(frames[1], p.Frame("digital", 5, 1))

    def test_serial_scan_limit_raises(self):
        # A serial header with no terminator, past the scan limit, must raise
        # so the caller drops the connection instead of buffering forever.
        header = bytes([0b11001000, 0])
        with self.assertRaises(p.ProtocolError):
            self.d.feed(header + b"x" * (p.SERIAL_SCAN_LIMIT + 1))


if __name__ == "__main__":
    unittest.main()
