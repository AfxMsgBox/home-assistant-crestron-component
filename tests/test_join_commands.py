"""Tests for join_commands: set-one-clear-others and the momentary pulse."""

import asyncio
import unittest

from loader import load

jc = load("join_commands")


class FakeHub:
    """Records set_digital calls in order; serves get_digital from state."""

    def __init__(self):
        self.calls = []  # list of (join, value) in call order
        self.state = {}  # join -> bool

    def set_digital(self, join, value):
        self.calls.append((join, bool(value)))
        self.state[join] = bool(value)


class SetOneClearOthersTests(unittest.TestCase):
    def test_sets_target_clears_rest(self):
        hub = FakeHub()
        jc.set_one_clear_others(hub, [1, 2, 3], 2)
        self.assertEqual(hub.state, {1: False, 2: True, 3: False})

    def test_clears_before_set(self):
        # The target must be the LAST call so no observer ever sees the previous
        # selection and the new one high simultaneously.
        hub = FakeHub()
        jc.set_one_clear_others(hub, [1, 2, 3], 2)
        self.assertEqual(hub.calls[-1], (2, True))
        # Every other join is cleared, and the target is never cleared.
        self.assertNotIn((2, False), hub.calls)
        self.assertIn((1, False), hub.calls)
        self.assertIn((3, False), hub.calls)

    def test_target_not_in_group_just_sets_it(self):
        hub = FakeHub()
        jc.set_one_clear_others(hub, [1, 2], 9)
        self.assertEqual(hub.state, {1: False, 2: False, 9: True})

    def test_accepts_arbitrary_iterable(self):
        hub = FakeHub()
        jc.set_one_clear_others(hub, {1, 2, 3}, 1)
        self.assertEqual(hub.state, {1: True, 2: False, 3: False})


class PulseDigitalTests(unittest.TestCase):
    def test_drives_high_then_low(self):
        hub = FakeHub()

        async def run():
            # Create the lock inside the loop (Python 3.9 binds it to the
            # running loop at construction time).
            await jc.pulse_digital(hub, asyncio.Lock(), 5, seconds=0)

        asyncio.run(run())
        self.assertEqual(hub.calls, [(5, True), (5, False)])

    def test_concurrent_presses_serialise(self):
        # Two overlapping presses on the same lock must not interleave: the
        # full high->low of one completes before the other starts.
        hub = FakeHub()

        async def run_both():
            lock = asyncio.Lock()
            await asyncio.gather(
                jc.pulse_digital(hub, lock, 7, seconds=0),
                jc.pulse_digital(hub, lock, 7, seconds=0),
            )

        asyncio.run(run_both())
        self.assertEqual(
            hub.calls, [(7, True), (7, False), (7, True), (7, False)]
        )


if __name__ == "__main__":
    unittest.main()
