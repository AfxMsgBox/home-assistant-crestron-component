"""Tests for the dimmable light's turn_off command sequence.

light.py imports homeassistant, which isn't installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before
loading the module via the synthetic-package loader. voluptuous is real.

async_turn_off must re-assert the current level and then send 0 (a distinct
high->0 edge), so the Crestron dimmer — which acts only on level *changes* —
reliably turns the bulb off on a single press.
"""

import asyncio
import enum
import sys
import types
import unittest

from loader import load


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class ColorMode(str, enum.Enum):
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"


class LightEntity:
    write_count = 0

    def async_write_ha_state(self):
        self.write_count += 1


class RestoreEntity:
    async def async_get_last_state(self):
        return None


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    components = _module("homeassistant.components")
    light = _module(
        "homeassistant.components.light",
        ColorMode=ColorMode, LightEntity=LightEntity,
    )
    _module("homeassistant.const", CONF_NAME="name", CONF_TYPE="type")
    ha.helpers = helpers
    ha.components = components
    components.light = light


_install_stubs()
light_mod = load("light")
# Keep the test fast: collapse the re-assert hold to a no-op delay.
light_mod.OFF_REASSERT_SECONDS = 0


class FakeHub:
    def __init__(self):
        self.analog = {}
        self.sent_analog = []  # (join, value) commands sent to the wire

    def get_analog(self, join):
        return self.analog.get(join, 0)

    def set_analog(self, join, value):
        # Mirror the real hub: writing the wire does NOT touch the read cache.
        self.sent_analog.append((join, value))

    def is_available(self):
        return True

    def register_callback(self, cb, joins=None):
        pass

    def remove_callback(self, cb):
        pass


def make_light(hub):
    return light_mod.CrestronLight(hub, {
        "name": "Ceiling", "type": "brightness", "brightness_join": 20,
    })


class TurnOffSequenceTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.light = make_light(self.hub)

    def test_reasserts_level_then_zero(self):
        # Light on (e.g. switched on at the keypad): re-assert the level, then 0,
        # so the dimmer sees an unambiguous high->0 edge and turns off at once.
        self.hub.analog[20] = 65535
        asyncio.run(self.light.async_turn_off())
        self.assertEqual(self.hub.sent_analog, [(20, 65535), (20, 0)])

    def test_already_off_sends_only_zero(self):
        # Current level 0: nothing to re-assert, just send 0 (no hold/delay).
        self.hub.analog[20] = 0
        asyncio.run(self.light.async_turn_off())
        self.assertEqual(self.hub.sent_analog, [(20, 0)])

    def test_turn_on_cancels_pending_delayed_zero(self):
        old_delay = light_mod.OFF_REASSERT_SECONDS
        light_mod.OFF_REASSERT_SECONDS = 0.05

        async def run():
            self.hub.analog[20] = 65535
            off_task = asyncio.create_task(self.light.async_turn_off())
            await asyncio.sleep(0.01)
            await self.light.async_turn_on()
            await off_task

        try:
            asyncio.run(run())
        finally:
            light_mod.OFF_REASSERT_SECONDS = old_delay

        self.assertEqual(self.hub.sent_analog, [(20, 65535)])


if __name__ == "__main__":
    unittest.main()
