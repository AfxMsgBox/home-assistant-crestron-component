"""调光灯开关电平变化、亮度记忆及并发命令回归。

使用最小 HA 替身和真实 voluptuous，避免连接现场；主控替身区分实际亮度、
HA 输入电平与回报缓存，验证实体开关关灯后同亮度再次开灯。
"""

import asyncio
import enum
import json
import sys
import types
import unittest
from unittest.mock import patch

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
OPEN_DELAY_SECONDS = light_mod.ON_RESET_SECONDS
light_mod.ON_RESET_SECONDS = 0


class FakeHub:
    def __init__(self):
        self.analog = {}
        self.sent_analog = []  # (join, value) commands sent to the wire
        self.digital = {}
        self.sent_digital = []

    def get_digital(self, join):
        return self.digital.get(join, False)

    def has_digital(self, join):
        return join in self.digital

    def set_digital(self, join, value):
        self.sent_digital.append((join, value))

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
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 65535)])

    async def test_off_seeds_memory_before_zero_without_prior_callback(self):
        self.hub.analog[20] = 23456
        await self.light.async_turn_off()
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 23456)
        await self.feedback(0)
        self.hub.sent_analog.clear()
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 23456)])

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
        self.assertEqual(hub.sent_analog, [(20, 0), (20, 23456)])

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


class ChangeDrivenHub(FakeHub):
    """模拟只响应 HA 输入电平变化的主控；实体开关不改 HA 输入电平。"""

    def __init__(self):
        super().__init__()
        self.command_level = 0
        self.actual_level = 0

    def set_analog(self, join, value):
        super().set_analog(join, value)
        if join == 20 and value != self.command_level:
            self.command_level = value
            self.actual_level = value


class TurnOnSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = ChangeDrivenHub()
        self.light = make_light(self.hub)
        self.waits = asyncio.Queue()

    async def hold(self, seconds):
        release = asyncio.Event()
        await self.waits.put((seconds, release))
        await release.wait()

    async def feedback(self, value):
        self.hub.analog[20] = value
        await self.light.process_callback('a20', str(value))

    async def test_panel_off_then_same_level_turn_on_really_opens_light(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                await self.light.async_turn_on(brightness=128)
                level = self.hub.actual_level
                await self.feedback(level)
                # 实体开关关灯并回报零，但上次 HA 输入仍是同一个非零值。
                self.hub.actual_level = 0
                await self.feedback(0)
                self.assertFalse(self.light.is_on)
                self.assertEqual(self.hub.command_level, level)
                self.hub.sent_analog.clear()
                await self.light.async_turn_on(**({'brightness': 128} if explicit else {}))
                self.assertEqual(self.hub.actual_level, level)
                self.assertEqual(self.hub.sent_analog, [(20, 0), (20, level)])
                self.assertEqual(self.light.extra_state_attributes['last_nonzero_brightness'], level)
                # 命令不伪造状态；收到反馈后才显示开灯。
                self.assertFalse(self.light.is_on)
                await self.feedback(level)
                self.assertTrue(self.light.is_on)

    async def test_already_on_dimming_does_not_send_zero(self):
        await self.feedback(12345)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [])
        await self.light.async_turn_on(brightness=128)
        self.assertEqual(self.hub.sent_analog, [(20, round(128 / 255 * 65535))])

    async def test_zero_request_does_not_wait_or_clear_memory(self):
        await self.feedback(12345)
        with patch.object(light_mod.asyncio, 'sleep', self.hold):
            await self.light.async_turn_on(brightness=0)
        self.assertTrue(self.waits.empty())
        self.assertEqual(self.hub.sent_analog, [(20, 0)])
        self.assertEqual(self.light.extra_state_attributes['last_nonzero_brightness'], 12345)

    async def test_zero_is_held_before_sending_remembered_raw_level(self):
        await self.feedback(12345)
        await self.feedback(0)
        with patch.object(light_mod, 'ON_RESET_SECONDS', 0.2), patch.object(light_mod.asyncio, 'sleep', self.hold):
            task = asyncio.create_task(self.light.async_turn_on())
            seconds, release = await self.waits.get()
            self.assertEqual(seconds, 0.2)
            self.assertEqual(self.hub.sent_analog, [(20, 0)])
            self.assertFalse(task.done())
            release.set()
            await task
        self.assertEqual(self.hub.actual_level, 12345)

    async def test_new_off_cancels_pending_brightness_and_color_temperature(self):
        self.light = light_mod.CrestronLight(self.hub, {
            'name': '示例灯', 'brightness_join': 20, 'color_temp_join': 21})
        with patch.object(light_mod.asyncio, 'sleep', self.hold):
            task = asyncio.create_task(self.light.async_turn_on(brightness=128, color_temp_kelvin=4000))
            _, release = await self.waits.get()
            await self.light.async_turn_off()
            release.set()
            await task
        self.assertEqual(self.hub.actual_level, 0)
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 0)])

    async def test_new_on_supersedes_old_target_and_preserves_pending_memory(self):
        for request in ({'brightness': 64}, {}):
            with self.subTest(request=request):
                self.hub.sent_analog.clear()
                with patch.object(light_mod.asyncio, 'sleep', self.hold):
                    first = asyncio.create_task(self.light.async_turn_on(brightness=128))
                    _, first_release = await self.waits.get()
                    second = asyncio.create_task(self.light.async_turn_on(**request))
                    _, second_release = await self.waits.get()
                    first_release.set()
                    await first
                    self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 0)])
                    second_release.set()
                    await second
                level = round(request.get('brightness', 128) / 255 * 65535)
                self.assertEqual(self.hub.sent_analog[-1], (20, level))
                self.assertEqual(self.light.extra_state_attributes['last_nonzero_brightness'], level)

    async def test_old_feedback_during_wait_does_not_replace_sent_target_memory(self):
        with patch.object(light_mod.asyncio, 'sleep', self.hold):
            task = asyncio.create_task(self.light.async_turn_on(brightness=128))
            _, release = await self.waits.get()
            await self.feedback(65535)
            release.set()
            await task
        self.assertEqual(self.light.extra_state_attributes['last_nonzero_brightness'], round(128 / 255 * 65535))

    async def test_cancelled_turn_on_does_not_send_delayed_level(self):
        with patch.object(light_mod.asyncio, 'sleep', self.hold):
            task = asyncio.create_task(self.light.async_turn_on(brightness=128))
            await self.waits.get()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.hub.sent_analog, [(20, 0)])

    async def test_removal_cancels_pending_on_and_off_writes(self):
        for turn_on in (True, False):
            with self.subTest(turn_on=turn_on):
                self.light = make_light(self.hub)
                self.hub.sent_analog.clear()
                await self.feedback(0 if turn_on else 12345)
                with patch.object(light_mod.asyncio, 'sleep', self.hold):
                    task = asyncio.create_task(self.light.async_turn_on() if turn_on else self.light.async_turn_off())
                    _, release = await self.waits.get()
                    before = list(self.hub.sent_analog)
                    await self.light.async_will_remove_from_hass()
                    release.set()
                    await task
                self.assertEqual(self.hub.sent_analog, before)


class DigitalPowerDimmingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.light = light_mod.CrestronLight(self.hub, {
            "name": "调光灯", "brightness_join": 20, "color_temp_join": 21,
            "on_join": 30, "off_join": 31,
        })
        await self.light.async_added_to_hass()

    async def feedback(self, on, off, brightness=32768):
        self.hub.analog[20] = brightness
        self.hub.digital.update({30: on, 31: off})
        await self.light.process_callback("d30", str(on))

    async def test_digital_feedback_overrides_nonzero_or_zero_brightness(self):
        await self.feedback(False, True)
        self.assertFalse(self.light.is_on)
        self.assertEqual(self.light.brightness, 128)
        await self.feedback(True, False, brightness=0)
        self.assertTrue(self.light.is_on)

    async def test_missing_or_ambiguous_power_never_falls_back_to_brightness(self):
        self.hub.analog[20] = 65535
        self.assertIsNone(self.light.is_on)
        await self.feedback(True, True)
        self.assertIsNone(self.light.is_on)
        await self.feedback(False, True)
        await self.feedback(False, False)
        self.assertFalse(self.light.is_on)
        await self.feedback(True, False)
        await self.feedback(True, True)
        self.assertTrue(self.light.is_on)

    async def test_on_restores_brightness_off_only_pulses_and_waits_for_feedback(self):
        await self.feedback(False, True)
        await self.light.async_turn_on()
        self.assertFalse(self.light.is_on)
        await self.feedback(True, False)
        await self.light.async_turn_off()
        self.assertTrue(self.light.is_on)
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 32768)])
        self.assertEqual(self.hub.sent_digital,
                         [(31, True), (31, False)])

    async def test_reset_is_held_for_tenth_second_before_restoring_memory(self):
        await self.feedback(False, True)

        async def wait(seconds):
            self.assertEqual(seconds, 0.1)
            self.assertEqual(self.hub.sent_analog, [(20, 0)])
            # 模拟主控在等待期间回传零；目标记忆仍应保留。
            self.hub.analog[20] = 0
            await self.light.process_callback("a20", "0")
            self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 32768)

        with patch.object(light_mod, "ON_RESET_SECONDS", OPEN_DELAY_SECONDS):
            with patch.object(light_mod.asyncio, "sleep", wait):
                await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 32768)])

    async def test_missing_or_zero_saved_level_opens_at_full_without_power_on(self):
        for controls in ({"on_join": 30, "off_join": 31}, {"switch_join": 32}):
            for saved in (None, 0):
                with self.subTest(controls=controls, saved=saved):
                    hub = FakeHub()
                    light = light_mod.CrestronLight(hub, {
                        "name": "灯", "brightness_join": 20, **controls})
                    light._last_state = types.SimpleNamespace(
                        state="off", attributes={"last_nonzero_brightness": saved})
                    await light.async_added_to_hass()
                    await light.async_turn_off()
                    self.assertEqual(hub.sent_analog, [])
                    hub.sent_digital.clear()
                    await light.async_turn_on()
                    self.assertEqual(hub.sent_digital, [])
                    self.assertEqual(hub.sent_analog, [(20, 0), (20, 65535)])
                    self.assertEqual(light.extra_state_attributes["last_nonzero_brightness"], 65535)

    async def test_saved_nonzero_level_survives_zero_feedback_and_power_cycle(self):
        self.light._last_state = types.SimpleNamespace(
            state="off", attributes={"last_nonzero_brightness": 23456})
        await self.light.async_added_to_hass()
        await self.feedback(False, True, brightness=0)
        await self.light.process_callback("a20", "0")
        await self.light.async_turn_on()
        await self.light.async_turn_off()
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 23456)])
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 23456)

    async def test_off_seeds_memory_without_changing_current_brightness(self):
        await self.feedback(True, False, brightness=23456)
        await self.light.async_turn_off()
        await self.feedback(False, True, brightness=0)
        await self.light.process_callback("a20", "0")
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 23456)])
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 23456)

    async def test_explicit_dimming_while_off_uses_only_analog(self):
        await self.feedback(False, True)
        await self.light.async_turn_on(brightness=64, color_temp_kelvin=4000)
        self.assertEqual(self.hub.sent_analog,
                         [(20, 0),
                          (20, round(64 / 255 * 65535)), (21, 4000)])
        self.assertEqual(self.hub.sent_digital, [])
        self.assertFalse(self.light.is_on)

    async def test_already_on_dimming_and_color_do_not_pulse_power(self):
        await self.feedback(True, False)
        await self.light.async_turn_on(brightness=128)
        await self.light.async_turn_on(color_temp_kelvin=4500)
        self.assertEqual(self.hub.sent_digital, [])
        self.assertEqual(self.hub.sent_analog, [(20, 32896), (21, 4500)])

    async def test_zero_brightness_uses_power_off_without_erasing_memory(self):
        await self.light.async_turn_on(brightness=64)
        self.hub.sent_analog.clear()
        self.hub.sent_digital.clear()
        await self.light.async_turn_on(brightness=0)
        self.assertEqual(self.hub.sent_analog, [])
        self.assertEqual(self.hub.sent_digital, [(31, True), (31, False)])
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"],
                         round(64 / 255 * 65535))

    async def test_held_switch_and_dedicated_feedback_priority(self):
        light = light_mod.CrestronLight(self.hub, {
            "name": "灯", "brightness_join": 20, "switch_join": 32,
            "state_join": 33,
        })
        self.hub.analog[20] = 65535
        self.hub.digital[32] = True
        self.assertIsNone(light.is_on)
        self.hub.digital[33] = False
        self.assertFalse(light.is_on)
        await light.async_turn_on()
        await light.async_turn_off()
        self.assertEqual(self.hub.sent_digital, [(32, False)])
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 65535)])
        self.assertEqual(light._callback_joins(), ["a20", "d33"])
        without_state = light_mod.CrestronLight(self.hub, {
            "name": "灯", "brightness_join": 20, "switch_join": 32})
        self.assertTrue(without_state.is_on)
        self.hub.digital[32] = False
        self.assertFalse(without_state.is_on)

    async def test_callback_joins_and_unique_id_stay_consistent(self):
        self.assertEqual(self.light._callback_joins(), ["a20", "a21", "d30", "d31"])
        self.assertEqual(self.light._attr_unique_id, make_light(self.hub)._attr_unique_id)

    async def test_power_pulses_serialize_and_release_on_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(seconds):
            entered.set()
            await release.wait()

        with patch.object(light_mod.asyncio, "sleep", hold):
            first = asyncio.create_task(self.light.async_turn_off())
            await entered.wait()
            second = asyncio.create_task(self.light.async_turn_off())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            await second
        self.assertEqual(self.hub.sent_digital,
                         [(31, True), (31, False), (31, True), (31, False)])

    async def test_removal_drops_queued_power_command(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(seconds):
            entered.set()
            await release.wait()

        with patch.object(light_mod.asyncio, "sleep", hold):
            first = asyncio.create_task(self.light.async_turn_off())
            await entered.wait()
            second = asyncio.create_task(self.light.async_turn_on(brightness=128))
            await self.light.async_will_remove_from_hass()
            release.set()
            await first
            await second
        self.assertEqual(self.hub.sent_digital, [(31, True), (31, False)])
        self.assertEqual(self.hub.sent_analog, [])

    async def test_new_off_cancels_queued_analog_on_and_color(self):
        # 模拟上一条命令占用锁；更新的关灯应淘汰排队中的开灯及色温。
        await self.light._power_command_lock.acquire()
        first = asyncio.create_task(self.light.async_turn_on(
            brightness=102, color_temp_kelvin=4000))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.light.async_turn_off())
        await asyncio.sleep(0)
        self.light._power_command_lock.release()
        await asyncio.gather(first, second)
        self.assertEqual(self.hub.sent_analog, [])
        self.assertEqual(self.hub.sent_digital, [(31, True), (31, False)])

    async def test_bulk_on_resets_then_restores_each_memory_without_digital_on(self):
        lights = []
        expected = []
        for index in range(10):
            join = 100 + index
            light = light_mod.CrestronLight(self.hub, {
                "name": f"灯{index}", "brightness_join": join,
                "on_join": 200 + index * 2, "off_join": 201 + index * 2})
            level = 5000 + index * 4000
            light._last_state = types.SimpleNamespace(
                state="off", attributes={"last_nonzero_brightness": level})
            await light.async_added_to_hass()
            self.hub.digital.update({200 + index * 2: False, 201 + index * 2: True})
            lights.append(light)
            expected.extend([(join, 0), (join, level)])
        await asyncio.gather(*(light.async_turn_on() for light in lights))
        self.assertCountEqual(self.hub.sent_analog, expected)
        self.assertEqual(self.hub.sent_digital, [])

    async def test_lowest_and_full_memory_survive_zero_before_on(self):
        for level in (1, 65535):
            with self.subTest(level=level):
                await self.feedback(False, True, brightness=0)
                self.light._remember_brightness(level)
                self.hub.sent_analog.clear()
                await self.light.async_turn_on()
                self.assertEqual(self.hub.sent_analog, [(20, 0), (20, level)])
                self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], level)

    async def test_new_off_stops_delayed_on_and_color_without_erasing_memory(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(seconds):
            entered.set()
            await release.wait()

        await self.feedback(False, True, brightness=0)
        with patch.object(light_mod.asyncio, "sleep", hold):
            first = asyncio.create_task(self.light.async_turn_on(
                brightness=102, color_temp_kelvin=4000))
            await entered.wait()
            self.hub.analog[20] = 0
            await self.light.process_callback("a20", "0")
            self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 26214)
            second = asyncio.create_task(self.light.async_turn_off())
            ready = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(ready.set_result, None)
            await ready
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(self.hub.sent_analog, [(20, 0)])
        self.assertEqual(self.hub.sent_digital, [(31, True), (31, False)])
        self.assertEqual(self.light.extra_state_attributes["last_nonzero_brightness"], 26214)

    async def test_cancel_or_remove_stops_delayed_on(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                hub = FakeHub()
                light = light_mod.CrestronLight(hub, {
                    "name": "灯", "brightness_join": 20,
                    "on_join": 30, "off_join": 31})
                entered, release = asyncio.Event(), asyncio.Event()

                async def hold(seconds):
                    entered.set()
                    await release.wait()

                with patch.object(light_mod.asyncio, "sleep", hold):
                    task = asyncio.create_task(light.async_turn_on(brightness=102))
                    await entered.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await light.async_will_remove_from_hass()
                        release.set()
                        await task
                self.assertEqual(hub.sent_analog, [(20, 0)])

    async def test_on_after_off_restores_even_if_digital_feedback_is_still_on(self):
        await self.feedback(True, False, brightness=26214)
        await self.light.async_turn_off()
        self.assertTrue(self.light.is_on)
        await self.light.async_turn_on()
        self.assertEqual(self.hub.sent_analog, [(20, 0), (20, 26214)])
        self.assertEqual(self.hub.sent_digital, [(31, True), (31, False)])

    async def test_panel_reopens_after_confirmed_off_then_dimming_does_not_reset(self):
        await self.feedback(True, False, brightness=26214)
        await self.light.async_turn_off()
        await self.feedback(False, True, brightness=0)
        await self.feedback(True, False, brightness=26214)
        await self.light.async_turn_on(brightness=128)
        self.assertEqual(self.hub.sent_analog, [(20, 32896)])

    async def test_controller_with_full_brightness_on_button_reopens_at_memory(self):
        class Hub(ChangeDrivenHub):
            def set_digital(self, join, value):
                super().set_digital(join, value)
                if value:
                    self.actual_level = 65535 if join == 30 else 0

        hub = Hub()
        light = light_mod.CrestronLight(hub, {
            "name": "灯", "brightness_join": 20, "on_join": 30, "off_join": 31})

        async def feedback():
            hub.analog[20] = hub.actual_level
            hub.digital.update({30: hub.actual_level > 0, 31: hub.actual_level == 0})
            await light.process_callback("a20", str(hub.actual_level))
            await light.process_callback("d30", str(hub.actual_level > 0))

        await light.async_turn_on()
        await feedback()
        await light.async_turn_on(brightness=102)
        await feedback()
        self.assertEqual(hub.actual_level, 26214)
        await light.async_turn_off()
        await feedback()
        self.assertEqual(hub.command_level, 26214)
        self.assertFalse(light.is_on)
        await light.async_turn_on()
        await feedback()
        self.assertEqual(hub.actual_level, 26214)
        self.assertTrue(light.is_on)
        self.assertEqual(light.extra_state_attributes["last_nonzero_brightness"], 26214)
        self.assertNotIn((30, True), hub.sent_digital)


if __name__ == "__main__":
    unittest.main()
