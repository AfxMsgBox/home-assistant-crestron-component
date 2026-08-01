"""Tests for pulse-mode switch feedback on on/off command joins."""

import asyncio
import enum
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from loader import load


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class SwitchEntity:
    write_count = 0

    def async_write_ha_state(self):
        self.write_count += 1


class RestoreEntity:
    async def async_get_last_state(self):
        return None


class SwitchDeviceClass(str, enum.Enum):
    OUTLET = "outlet"
    SWITCH = "switch"


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    components = _module("homeassistant.components")
    switch = _module(
        "homeassistant.components.switch",
        SwitchDeviceClass=SwitchDeviceClass,
        SwitchEntity=SwitchEntity,
    )
    _module(
        "homeassistant.const",
        CONF_NAME="name",
        CONF_DEVICE_CLASS="device_class",
    )
    ha.helpers = helpers
    ha.components = components
    components.switch = switch


_install_stubs()
switch_mod = load("switch")


class FakeHub:
    def __init__(self):
        self.digital = {}
        self.sent_digital = []

    def get_digital(self, join):
        return self.digital.get(join, False)

    def has_digital(self, join):
        return join in self.digital

    def set_digital(self, join, value):
        self.sent_digital.append((join, bool(value)))

    def is_available(self):
        return True

    def register_callback(self, cb, joins=None):
        self.registered_joins = joins

    def remove_callback(self, cb):
        pass


def make_switch(hub):
    return switch_mod.CrestronSwitch(hub, {
        "name": "Outlet", "on_join": 147, "off_join": 148,
        "device_class": "outlet",
    })


def make_mode_switch(hub):
    return switch_mod.CrestronSwitch(hub, {
        "name": "AC power",
        "on_join": 147,
        "off_join": 148,
        "mode_joins": {"制冷": 507, "制热": 508},
    })


class SwitchFeedbackTests(unittest.TestCase):
    def setUp(self):
        try:
            self.previous_loop = asyncio.get_running_loop()
        except RuntimeError:
            self.previous_loop = None
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.hub = FakeHub()
        self.switch = make_switch(self.hub)
        self.run_async(self.switch.async_added_to_hass())

    def tearDown(self):
        self.loop.close()
        if self.previous_loop is not None:
            asyncio.set_event_loop(self.previous_loop)
        else:
            asyncio.set_event_loop(None)

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def test_subscribes_to_both_command_joins(self):
        self.assertEqual(set(self.hub.registered_joins), {"d147", "d148"})

    def test_external_on_reflected(self):
        self.hub.digital[147] = True
        self.hub.digital[148] = False
        self.run_async(self.switch.process_callback("d147", "1"))
        self.assertTrue(self.switch.is_on)

    def test_external_off_reflected(self):
        self.hub.digital[147] = True
        self.hub.digital[148] = False
        self.run_async(self.switch.process_callback("d147", "1"))
        self.assertTrue(self.switch.is_on)

        self.hub.digital[147] = False
        self.hub.digital[148] = True
        self.run_async(self.switch.process_callback("d148", "1"))
        self.assertFalse(self.switch.is_on)

    def test_both_high_keeps_current_state(self):
        self.hub.digital[147] = True
        self.run_async(self.switch.process_callback("d147", "1"))
        self.assertTrue(self.switch.is_on)

        self.hub.digital[148] = True
        self.run_async(self.switch.process_callback("d148", "1"))
        self.assertTrue(self.switch.is_on)

    def test_both_low_keeps_current_state(self):
        self.switch._optimistic_state = True
        self.run_async(self.switch.process_callback("d147", "0"))
        self.assertTrue(self.switch.is_on)

    def test_turn_on_pulses_on_join_and_shows_on(self):
        self.run_async(self.switch.async_turn_on())
        self.assertTrue(self.switch.is_on)
        self.assertIn((147, True), self.hub.sent_digital)
        self.assertIn((147, False), self.hub.sent_digital)

    def test_unique_id_does_not_include_name(self):
        renamed = switch_mod.CrestronSwitch(
            self.hub,
            {
                "name": "Renamed outlet",
                "on_join": 147,
                "off_join": 148,
                "device_class": "outlet",
            },
        )
        self.assertEqual(self.switch._attr_unique_id, "crestron_switch_d147")
        self.assertEqual(renamed._attr_unique_id, self.switch._attr_unique_id)


class SwitchModeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.switch = make_mode_switch(self.hub)

    def test_partial_all_low_is_still_unknown(self):
        """One low frame cannot prove off while another join is unreported."""
        self.hub.digital[507] = False
        self.switch._optimistic_state = True

        self.assertIsNone(self.switch._feedback_is_on())
        self.assertTrue(self.switch.is_on)
        self.assertIsNone(self.switch.extra_state_attributes)

    def test_high_join_is_definitive_before_full_sync_finishes(self):
        self.hub.digital[508] = True

        self.assertTrue(self.switch._feedback_is_on())
        self.assertEqual(
            self.switch.extra_state_attributes, {"mode": "制热"}
        )

    def test_all_reported_low_is_off(self):
        self.hub.digital[507] = False
        self.hub.digital[508] = False

        self.assertFalse(self.switch._feedback_is_on())
        self.assertEqual(
            self.switch.extra_state_attributes, {"mode": "关闭"}
        )


if __name__ == "__main__":
    unittest.main()
