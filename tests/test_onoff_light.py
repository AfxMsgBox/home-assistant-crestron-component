"""Tests for the on/off (relay-style) light's state feedback.

light.py imports homeassistant, which isn't installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before
loading the module via the synthetic-package loader. voluptuous is real.

The pulse-only light (on_join/off_join, no dedicated state_join) reads its
on/off state back from the command joins themselves: on_join high = on,
off_join high = off. These tests cover that reconciliation.
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


class FakeHub:
    def __init__(self):
        self.digital = {}
        self.sent_digital = []  # (join, value) commands sent to the wire

    def get_digital(self, join):
        return self.digital.get(join, False)

    def get_analog(self, join):
        return 0

    def set_digital(self, join, value):
        # Mirror the real hub: writing the wire does NOT touch the read cache.
        self.sent_digital.append((join, bool(value)))

    def set_analog(self, join, value):
        pass

    def is_available(self):
        return True

    def register_callback(self, cb, joins=None):
        self.registered_joins = joins

    def remove_callback(self, cb):
        pass


def make_light(hub):
    return light_mod.CrestronOnOffLight(hub, {
        "name": "Garage", "on_join": 10, "off_join": 11,
    })


class OnOffFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.light = make_light(self.hub)
        asyncio.run(self.light.async_added_to_hass())

    def test_subscribes_to_both_command_joins(self):
        # With no dedicated state join, feedback rides the on/off command joins.
        self.assertEqual(set(self.hub.registered_joins), {"d10", "d11"})

    def test_external_on_reflected(self):
        # Panel turns the light on: on_join goes high, off_join low.
        self.hub.digital[10] = True
        self.hub.digital[11] = False
        asyncio.run(self.light.process_callback("d10", "1"))
        self.assertTrue(self.light.is_on)

    def test_external_off_reflected(self):
        self.hub.digital[10] = True
        asyncio.run(self.light.process_callback("d10", "1"))
        self.assertTrue(self.light.is_on)
        # Now turned off externally: off_join high, on_join low.
        self.hub.digital[10] = False
        self.hub.digital[11] = True
        asyncio.run(self.light.process_callback("d11", "1"))
        self.assertFalse(self.light.is_on)

    def test_both_high_keeps_current_state(self):
        # Mid-transition both joins read high: indeterminate, keep last state.
        self.hub.digital[10] = True
        asyncio.run(self.light.process_callback("d10", "1"))
        self.assertTrue(self.light.is_on)
        self.hub.digital[11] = True  # off_join also momentarily high
        asyncio.run(self.light.process_callback("d11", "1"))
        self.assertTrue(self.light.is_on)

    def test_both_low_keeps_current_state(self):
        # Nothing reported yet (both low): don't force off.
        self.light._optimistic_state = True
        asyncio.run(self.light.process_callback("d10", "0"))
        self.assertTrue(self.light.is_on)

    def test_turn_on_pulses_on_join_and_shows_on(self):
        asyncio.run(self.light.async_turn_on())
        self.assertTrue(self.light.is_on)
        self.assertIn((10, True), self.hub.sent_digital)
        self.assertIn((10, False), self.hub.sent_digital)  # pulse released

    def test_unique_id_is_stable_across_rename(self):
        renamed = light_mod.CrestronOnOffLight(
            self.hub,
            {"name": "Renamed", "on_join": 10, "off_join": 11},
        )
        self.assertEqual(self.light._attr_unique_id, "crestron_light_onoff_d10")
        self.assertEqual(renamed._attr_unique_id, self.light._attr_unique_id)

    def test_optional_state_join_does_not_change_id(self):
        with_feedback = light_mod.CrestronOnOffLight(
            self.hub,
            {
                "name": "Garage",
                "on_join": 10,
                "off_join": 11,
                "state_join": 12,
            },
        )
        self.assertEqual(with_feedback._attr_unique_id, self.light._attr_unique_id)


if __name__ == "__main__":
    unittest.main()
