"""Tests for CrestronEntity's coalesced state writes.

The control system pushes each join as its own frame, so an entity watching N
joins is called back N times for what is really one update. Reconciling is
cheap; async_write_ha_state() is not (State object + bus event + recorder).
The mixin therefore defers the write to the next event-loop iteration and
collapses a burst into a single write.

Only feedback-driven writes are coalesced — command paths still write
immediately so the optimistic state shows with no delay.
"""

import asyncio
import unittest

from loader import load

entity_mod = load("entity")


class FakeHass:
    def __init__(self, loop):
        self.loop = loop


class FakeHub:
    def __init__(self):
        self.removed = []

    def register_callback(self, cb, joins):
        pass

    def remove_callback(self, cb):
        self.removed.append(cb)

    def is_available(self):
        return True


class Entity(entity_mod.CrestronEntity):
    """Minimal stand-in: the mixin only needs _hub and async_write_ha_state."""

    def __init__(self, hub, hass=None):
        self._hub = hub
        self.writes = 0
        if hass is not None:
            self.hass = hass

    def async_write_ha_state(self):
        self.writes += 1


class CoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.hass = FakeHass(asyncio.get_running_loop())
        self.entity = Entity(self.hub, self.hass)

    async def _settle(self):
        """Let the loop run the scheduled flush."""
        await asyncio.sleep(0)

    async def test_burst_of_joins_writes_once(self):
        for cbtype in ("d505", "d506", "d507", "a414", "a415"):
            await self.entity.process_callback(cbtype, "1")
        self.assertEqual(self.entity.writes, 0, "write should be deferred")
        await self._settle()
        self.assertEqual(self.entity.writes, 1)

    async def test_later_burst_writes_again(self):
        """Coalescing must not swallow updates that arrive after the flush."""
        await self.entity.process_callback("d1", "1")
        await self._settle()
        await self.entity.process_callback("d1", "0")
        await self._settle()
        self.assertEqual(self.entity.writes, 2)

    async def test_removal_cancels_pending_write(self):
        """Writing state after removal raises in Home Assistant."""
        await self.entity.process_callback("d1", "1")
        await self.entity.async_will_remove_from_hass()
        await self._settle()
        self.assertEqual(self.entity.writes, 0)
        self.assertEqual(self.hub.removed, [self.entity.process_callback])

    async def test_detached_entity_writes_immediately(self):
        """Without hass there is no loop to coalesce against (unit tests)."""
        detached = Entity(self.hub)
        await detached.process_callback("d1", "1")
        self.assertEqual(detached.writes, 1)


if __name__ == "__main__":
    unittest.main()
