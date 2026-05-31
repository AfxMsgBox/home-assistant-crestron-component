"""End-to-end tests for the XSIG TCP protocol in crestron.py.

These spin up the real ``CrestronXsig`` server on an ephemeral port, connect a
plain TCP client, and exercise inbound parsing, outbound serialization,
callback routing/isolation and availability tracking. No Home Assistant
runtime is required.
"""

import asyncio
import logging
import unittest

from loader import load

xsig = load("crestron")
CrestronXsig = xsig.CrestronXsig

# The callback-isolation and out-of-range tests intentionally trigger the
# component's own warning/exception logging. Silence it so expected noise
# (including a deliberate traceback) doesn't clutter the test output.
logging.getLogger(xsig.__name__).setLevel(logging.CRITICAL)


# --- Independent wire-format encoders (mirror the protocol, not the impl) ---

def enc_digital(join, value):
    flag = 0b00100000 if not value else 0
    return bytes([0b10000000 | flag | ((join - 1) >> 7), (join - 1) & 0x7F])


def enc_analog(join, value):
    return bytes(
        [
            0b11000000 | ((value >> 10) & 0b00110000) | ((join - 1) >> 7),
            (join - 1) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        ]
    )


def enc_serial(join, text):
    body = text.encode("utf-8")
    return bytes([0b11001000 | ((join - 1) >> 7), (join - 1) & 0x7F]) + body + b"\xff"


async def _wait_for(predicate, timeout=2.0, interval=0.005):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class XsigServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = CrestronXsig()
        await self.hub.listen(0)  # ephemeral port
        self.port = self.hub._server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.port
        )
        # Server greets every connection with 0xFD (sync-all-joins request).
        hello = await asyncio.wait_for(self.reader.readexactly(1), timeout=2)
        self.assertEqual(hello, b"\xfd")
        await _wait_for(self.hub.is_available)
        self.assertTrue(self.hub.is_available())

    async def asyncTearDown(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
        await self.hub.stop()

    # ---- inbound parsing ----

    async def test_inbound_digital(self):
        got = []

        async def cb(t, v):
            got.append((t, v))

        self.hub.register_callback(cb, joins=["d5"])
        self.writer.write(enc_digital(5, 1))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_digital(5))
        self.assertTrue(self.hub.get_digital(5))
        await _wait_for(lambda: ("d5", "1") in got)
        self.assertIn(("d5", "1"), got)

    async def test_inbound_digital_off(self):
        self.writer.write(enc_digital(5, 1))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_digital(5))
        self.writer.write(enc_digital(5, 0))
        await self.writer.drain()
        await _wait_for(lambda: not self.hub.get_digital(5))
        self.assertFalse(self.hub.get_digital(5))

    async def test_inbound_analog(self):
        self.writer.write(enc_analog(3, 1000))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_analog(3) == 1000)
        self.assertEqual(self.hub.get_analog(3), 1000)

    async def test_inbound_analog_full_scale(self):
        self.writer.write(enc_analog(1, 65535))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_analog(1) == 65535)
        self.assertEqual(self.hub.get_analog(1), 65535)

    async def test_inbound_serial_utf8(self):
        self.writer.write(enc_serial(4, "你好 hi"))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_serial(4) == "你好 hi")
        self.assertEqual(self.hub.get_serial(4), "你好 hi")

    async def test_inbound_frame_split_across_reads(self):
        # Deliver an analog frame one byte at a time; the reader must reassemble.
        for b in enc_analog(7, 12345):
            self.writer.write(bytes([b]))
            await self.writer.drain()
            await asyncio.sleep(0.003)
        await _wait_for(lambda: self.hub.get_analog(7) == 12345)
        self.assertEqual(self.hub.get_analog(7), 12345)

    # ---- outbound serialization ----

    async def test_outbound_digital(self):
        self.hub.set_digital(10, True)
        data = await asyncio.wait_for(self.reader.readexactly(2), timeout=2)
        self.assertEqual(data, enc_digital(10, True))

    async def test_outbound_analog_clamps(self):
        self.hub.set_analog(2, 70000)  # over range -> clamps to 65535
        data = await asyncio.wait_for(self.reader.readexactly(4), timeout=2)
        self.assertEqual(data, enc_analog(2, 65535))

    async def test_outbound_serial(self):
        self.hub.set_serial(6, "hi")
        data = await asyncio.wait_for(self.reader.readuntil(b"\xff"), timeout=2)
        self.assertEqual(data, enc_serial(6, "hi"))

    async def test_outbound_serial_too_long_is_dropped(self):
        self.hub.set_serial(1, "x" * 253)  # 253 bytes > 252 limit
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.reader.readexactly(1), timeout=0.3)

    async def test_outbound_join_out_of_range_is_dropped(self):
        self.hub.set_analog(9999, 5)  # > ANALOG_JOIN_MAX
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.reader.readexactly(1), timeout=0.3)

    # ---- control bytes ----

    async def test_sync_all_joins_request(self):
        called = asyncio.Event()

        async def on_sync():
            called.set()

        self.hub.register_sync_all_joins_callback(on_sync)
        self.writer.write(b"\xfb")
        await self.writer.drain()
        await asyncio.wait_for(called.wait(), timeout=2)
        self.assertTrue(called.is_set())

    # ---- callback routing & isolation ----

    async def test_per_join_callback_filtering(self):
        got = []

        async def cb(t, v):
            got.append(t)

        self.hub.register_callback(cb, joins=["d5"])
        self.writer.write(enc_digital(6, 1))  # a join we did NOT subscribe to
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_digital(6))
        self.assertNotIn("d6", got)
        self.writer.write(enc_digital(5, 1))
        await self.writer.drain()
        await _wait_for(lambda: "d5" in got)
        self.assertIn("d5", got)

    async def test_callback_isolation(self):
        order = []

        async def bad(t, v):
            order.append("bad")
            raise RuntimeError("boom")

        async def good(t, v):
            order.append("good")

        self.hub.register_callback(bad, joins=["d8"])
        self.hub.register_callback(good, joins=["d8"])
        self.writer.write(enc_digital(8, 1))
        await self.writer.drain()
        await _wait_for(lambda: "good" in order)
        self.assertIn("good", order)  # ran despite the other callback raising
        # The session survives: a later frame is still processed.
        self.writer.write(enc_analog(9, 42))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_analog(9) == 42)
        self.assertEqual(self.hub.get_analog(9), 42)

    async def test_remove_callback(self):
        got = []

        async def cb(t, v):
            got.append(t)

        self.hub.register_callback(cb, joins=["d5"])
        self.hub.remove_callback(cb)
        self.writer.write(enc_digital(5, 1))
        await self.writer.drain()
        await _wait_for(lambda: self.hub.get_digital(5))
        self.assertEqual(got, [])

    # ---- availability ----

    async def test_availability_debounce(self):
        events = []

        async def cb(t, v):
            if t == "available":
                events.append(v)

        self.hub.register_callback(cb)  # broadcast
        await self.hub._notify_available(True)  # already True -> no dispatch
        self.assertEqual(events, [])
        await self.hub._notify_available(False)
        await self.hub._notify_available(False)  # debounced
        self.assertEqual(events, ["False"])

    async def test_disconnect_marks_unavailable(self):
        self.assertTrue(self.hub.is_available())
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
        await _wait_for(lambda: not self.hub.is_available())
        self.assertFalse(self.hub.is_available())


if __name__ == "__main__":
    unittest.main()
