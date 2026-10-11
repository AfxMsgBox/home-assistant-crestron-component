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
import json
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
        return getattr(self, "_last_state", None)


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

    def has_analog(self, join):
        return join in self.analog

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

    def test_turn_on_to_brightness_wins_over_pending_zero(self):
        # Real-world "灯开后秒关": while turn_off holds before sending 0, the
        # user turns the light back on to a specific brightness. The pending 0
        # must be abandoned so the light ends at the new level, not off.
        old_delay = light_mod.OFF_REASSERT_SECONDS
        light_mod.OFF_REASSERT_SECONDS = 0.05

        async def run():
            self.hub.analog[20] = 65535
            off_task = asyncio.create_task(self.light.async_turn_off())
            await asyncio.sleep(0.01)
            await self.light.async_turn_on(brightness=128)
            await off_task

        try:
            asyncio.run(run())
        finally:
            light_mod.OFF_REASSERT_SECONDS = old_delay

        # Re-assert from turn_off, then the new level from turn_on; crucially
        # the delayed (20, 0) is never sent.
        self.assertEqual(
            self.hub.sent_analog,
            [(20, 65535), (20, round(128 / 255 * 65535))],
        )
        self.assertNotIn((20, 0), self.hub.sent_analog)


class BrightnessMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.light = make_light(self.hub)
        await self.light.async_added_to_hass()

    async def feedback(self, value, light=None, hub=None):
        light = light or self.light
        hub = hub or self.hub
        hub.analog[20] = value
        await light.process_callback("a20", str(value))

    async def test_set_half_off_on_restores_requested_brightness(self):
        level = round(128 / 255 * 65535)
        await self.light.async_turn_on(brightness=128)
        await self.feedback(level)
        await self.light.async_turn_off()
        await self.feedback(0)
        self.assertFalse(self.light.is_on)
        self.assertEqual(self.light.brightness, 0)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, level))

    async def test_external_feedback_keeps_exact_raw_level_through_off_and_on(self):
        for level in (1, 12345, 32768, 65535):
            with self.subTest(level=level):
                await self.feedback(level)
                await self.feedback(0)
                await self.light.async_turn_on()
                self.assertEqual(self.hub.sent_analog[-1], (20, level))

    async def test_new_feedback_replaces_requested_level(self):
        await self.light.async_turn_on(brightness=128)
        await self.feedback(20000)
        await self.feedback(0)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, 20000))

    async def test_command_without_feedback_is_remembered_and_stale_cache_cannot_replace_it(self):
        await self.feedback(65535)
        await self.light.async_turn_on(brightness=128)
        await self.light.async_turn_off()
        level = round(128 / 255 * 65535)
        # 关灯重发最新设定值，不重发旧反馈中的 100%。
        self.assertEqual(self.hub.sent_analog[-2:], [(20, level), (20, 0)])
        await self.feedback(0)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, level))

    async def test_zero_command_does_not_erase_memory(self):
        await self.light.async_turn_on(brightness=80)
        await self.light.async_turn_on(brightness=0)
        await self.feedback(0)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, round(80 / 255 * 65535)))

    async def test_no_record_defaults_to_full_brightness(self):
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [(20, 65535)])

    async def test_restart_while_off_restores_memory_without_turning_light_on(self):
        await self.feedback(23456)
        await self.feedback(0)
        # 标准亮度在关灯时可以为 None，记忆通过独立属性保存。
        saved = json.loads(json.dumps({**self.light.extra_state_attributes, "brightness": None}))
        hub = FakeHub()
        restored = make_light(hub)
        restored._last_state = types.SimpleNamespace(state="off", attributes=saved)
        await restored.async_added_to_hass()
        self.assertFalse(restored.is_on)
        self.assertEqual(hub.sent_analog, [])
        await restored.async_turn_on()
        self.assertEqual(hub.sent_analog, [(20, 23456)])

    async def test_initial_live_nonzero_feedback_takes_priority_over_saved_memory(self):
        hub = FakeHub()
        hub.analog[20] = 45678
        restored = make_light(hub)
        restored._last_state = types.SimpleNamespace(
            state="off", attributes={"last_nonzero_brightness": 20000})
        await restored.async_added_to_hass()
        await self.feedback(0, restored, hub)
        await restored.async_turn_on()
        self.assertEqual(hub.sent_analog[-1], (20, 45678))

    async def test_initial_zero_does_not_erase_saved_memory(self):
        hub = FakeHub()
        hub.analog[20] = 0
        restored = make_light(hub)
        restored._last_state = types.SimpleNamespace(
            state="off", attributes={"last_nonzero_brightness": 20000})
        await restored.async_added_to_hass()
        await restored.async_turn_on()
        self.assertEqual(hub.sent_analog[-1], (20, 20000))

    async def test_legacy_on_state_can_seed_memory(self):
        restored = make_light(self.hub)
        restored._last_state = types.SimpleNamespace(state="on", attributes={"brightness": 64})
        await restored.async_added_to_hass()
        await restored.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, round(64 / 255 * 65535)))

    async def test_invalid_restored_memory_is_ignored(self):
        for value in (None, True, 0, -1, 65536, 20.5, "25000", float("nan")):
            with self.subTest(value=value):
                restored = make_light(self.hub)
                restored._last_state = types.SimpleNamespace(
                    state="off", attributes={"last_nonzero_brightness": value})
                await restored.async_added_to_hass()
                await restored.async_turn_on()
                self.assertEqual(self.hub.sent_analog[-1], (20, 65535))

    async def test_feedback_during_restore_wins_over_history(self):
        restored = make_light(self.hub)

        async def load_state():
            await self.feedback(22222, restored)
            return types.SimpleNamespace(state="off", attributes={"last_nonzero_brightness": 11111})

        restored.async_get_last_state = load_state
        await restored.async_added_to_hass()
        await self.feedback(0, restored)
        await restored.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, 22222))

    async def test_unrelated_feedback_does_not_revive_old_brightness(self):
        self.hub.analog[20] = 65535
        await self.light.async_turn_on(brightness=50)
        await self.light.process_callback("a21", "4000")
        await self.light.process_callback("available", "True")
        await self.feedback(0)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, round(50 / 255 * 65535)))

    async def test_each_light_has_its_own_memory(self):
        other_hub = FakeHub()
        other = make_light(other_hub)
        await self.light.async_turn_on(brightness=40)
        await other.async_turn_on(brightness=90)
        await self.light.async_turn_on()
        await other.async_turn_on()
        self.assertEqual(self.hub.sent_analog[-1], (20, round(40 / 255 * 65535)))
        self.assertEqual(other_hub.sent_analog[-1], (20, round(90 / 255 * 65535)))

    async def test_color_temperature_preserves_on_level_and_restores_off_level(self):
        light = light_mod.CrestronLight(self.hub, {
            "name": "双色温", "brightness_join": 20, "color_temp_join": 21})
        await light.async_added_to_hass()
        await self.feedback(30000, light)
        await light.async_turn_on(color_temp_kelvin=4000)
        self.assertEqual(self.hub.sent_analog, [(21, 4000)])
        await self.feedback(0, light)
        await light.async_turn_on(color_temp_kelvin=4500)
        self.assertEqual(self.hub.sent_analog[-2:], [(20, 30000), (21, 4500)])

    async def test_removed_light_ignores_feedback_and_commands(self):
        await self.feedback(20000)
        await self.light.async_will_remove_from_hass()
        await self.feedback(65535)
        await self.light.async_turn_on(brightness=255)
        await self.light.async_turn_off()
        self.assertEqual(self.hub.sent_analog, [])
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 20000)


if __name__ == "__main__":
    unittest.main()
