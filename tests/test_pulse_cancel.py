"""A cancelled pulse must still release the join.

pulse_digital drives a join high, holds, then drives it low. A reload, a
shutdown, or any task cancellation inside the hold window used to skip the
release and leave the join driven high forever — a relay stuck energised.
Every pulse-driven platform (switch, on/off light, cover, climate) depends on
this.
"""

import asyncio
import unittest

from loader import load

jc = load("join_commands")


class FakeHub:
    def __init__(self):
        self.sent = []

    def set_digital(self, join, value):
        self.sent.append((join, bool(value)))

    def get_digital(self, join):
        return False


class PulseCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_pulse_releases(self):
        hub = FakeHub()
        await jc.pulse_digital(hub, asyncio.Lock(), 42, seconds=0.01)
        self.assertEqual(hub.sent, [(42, True), (42, False)])

    async def test_cancelled_pulse_still_releases(self):
        hub = FakeHub()
        task = asyncio.create_task(
            jc.pulse_digital(hub, asyncio.Lock(), 42, seconds=5)
        )
        await asyncio.sleep(0)  # let it drive the join high
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(hub.sent, [(42, True), (42, False)])
        self.assertFalse(hub.sent[-1][1], "join must not be left high")

    async def test_cancellation_still_propagates(self):
        """The release must not swallow the cancellation."""
        hub = FakeHub()
        task = asyncio.create_task(
            jc.pulse_digital(hub, asyncio.Lock(), 7, seconds=5)
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())

    async def test_lock_released_after_cancellation(self):
        """A cancelled pulse must not deadlock the entity's next press."""
        hub = FakeHub()
        lock = asyncio.Lock()
        task = asyncio.create_task(jc.pulse_digital(hub, lock, 7, seconds=5))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(lock.locked())
        await jc.pulse_digital(hub, lock, 7, seconds=0.01)
        self.assertEqual(hub.sent[-2:], [(7, True), (7, False)])


if __name__ == "__main__":
    unittest.main()
