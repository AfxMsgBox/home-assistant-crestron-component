"""Climate values, fresh feedback, and ordered command regression tests.

climate.py imports homeassistant, which isn't installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before
loading the module via the synthetic-package loader. voluptuous is real.
"""

import asyncio
import enum
import sys
import types
import unittest
from unittest.mock import patch

from loader import load


# --------------------------------------------------------------------------- #
# stub just enough of homeassistant/voluptuous for climate.py to import
# --------------------------------------------------------------------------- #
def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class HVACMode(str, enum.Enum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class HVACAction(str, enum.Enum):
    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DRYING = "drying"
    FAN = "fan"
    IDLE = "idle"


class ClimateEntityFeature(enum.IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    TURN_OFF = 256
    TURN_ON = 512


class ClimateEntity:
    # CrestronAC never calls super().__init__(), so default via class attr.
    write_count = 0

    def async_write_ha_state(self):
        self.write_count += 1


class RestoreEntity:
    async def async_get_last_state(self):
        return None


class UnitOfTemperature:
    CELSIUS = "°C"


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    components = _module("homeassistant.components")
    climate = _module(
        "homeassistant.components.climate",
        ClimateEntity=ClimateEntity, ClimateEntityFeature=ClimateEntityFeature,
        HVACMode=HVACMode, HVACAction=HVACAction,
    )
    _module("homeassistant.components.climate.const",
            FAN_LOW="low", FAN_MEDIUM="medium", FAN_HIGH="high", FAN_AUTO="auto")
    _module("homeassistant.const", CONF_NAME="name", ATTR_TEMPERATURE="temperature",
            UnitOfTemperature=UnitOfTemperature)
    ha.helpers = helpers
    ha.components = components
    components.climate = climate


_install_stubs()
climate_mod = load("climate")


class FakeHub:
    def __init__(self):
        self.analog = {}
        self.digital = {}
        self.sent_digital = []  # (join, value) commands sent to the wire
        self.sent_analog = []
        self.available = True

    def has_analog(self, join):
        return join in self.analog

    def get_analog(self, join):
        return self.analog.get(join, 0)

    def get_digital(self, join):
        return self.digital.get(join, False)

    def has_digital(self, join):
        return join in self.digital

    def set_digital(self, join, value):
        self.sent_digital.append((join, bool(value)))

    def set_analog(self, join, value):
        self.sent_analog.append((join, value))

    def is_available(self):
        return self.available

    def register_callback(self, cb, joins=None):
        self.registered_joins = joins

    def remove_callback(self, cb):
        pass


def make_ac(hub):
    return climate_mod.CrestronAC(hub, {
        "name": "AC", "on_join": 505, "off_join": 506,
        "set_temp_join": 414, "reg_temp_join": 415,
        "mode_cool_join": 507, "fan_low_join": 512,
    })


class TempFilterTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.ac = make_ac(self.hub)

    def _temp_event(self, value):
        self.hub.analog[415] = value
        asyncio.run(self.ac.process_callback("a415", str(value)))

    def test_first_value_reported(self):
        self._temp_event(24)
        self.assertEqual(self.ac.current_temperature, 24)
        self.assertEqual(self.ac.write_count, 1)

    def test_sub_threshold_change_dropped(self):
        self._temp_event(24)
        self._temp_event(24)      # duplicate push
        self._temp_event(24.4)    # < 0.5 delta
        self.assertEqual(self.ac.current_temperature, 24)
        self.assertEqual(self.ac.write_count, 1)

    def test_threshold_change_reported(self):
        self._temp_event(24)
        self._temp_event(24.5)
        self.assertEqual(self.ac.current_temperature, 24.5)
        self.assertEqual(self.ac.write_count, 2)

    def test_known_zero_reported(self):
        self._temp_event(0)
        self.assertEqual(self.ac.current_temperature, 0)
        self.assertEqual(self.ac.write_count, 1)

    def test_other_events_cannot_bypass_temperature_filter(self):
        self._temp_event(24)
        # A mode update must apply the same threshold as a temperature update.
        # Power was already on. A mode event cannot invent a power report.
        self.ac._optimistic_on = True
        self.hub.analog[415] = 24.3
        self.hub.digital[505] = True  # on_join feedback: powered on
        self.hub.digital[507] = True  # running in cool
        asyncio.run(self.ac.process_callback("d507", "1"))
        self.assertEqual(self.ac.write_count, 2)
        self.assertEqual(self.ac.current_temperature, 24)
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.COOL)

    def test_unknown_feedback_does_not_become_zero(self):
        asyncio.run(self.ac.async_added_to_hass())
        asyncio.run(self.ac.process_callback("d507", "0"))
        self.assertIsNone(self.ac.current_temperature)
        self.assertIsNone(self.ac.target_temperature)

    def test_room_temperature_can_return_to_zero(self):
        self._temp_event(24)
        self._temp_event(0)
        self.assertEqual(self.ac.current_temperature, 0)
        self.assertEqual(self.ac.write_count, 2)

    def test_target_feedback_is_not_clipped_to_command_range(self):
        for value in (24, 0, 35):
            with self.subTest(value=value):
                self.hub.analog[414] = value
                asyncio.run(self.ac.process_callback("a414", str(value)))
                self.assertEqual(self.ac.target_temperature, value)


class TemperatureCommandTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.ac = make_ac(self.hub)

    def test_valid_whole_temperatures_match_wire_value(self):
        for value in (16, 30, 25.0, "24.0"):
            with self.subTest(value=value):
                asyncio.run(self.ac.async_set_temperature(temperature=value))
                self.assertEqual(self.ac.target_temperature, int(float(value)))
                self.assertEqual(self.hub.sent_analog[-1],
                                 (414, self.ac.target_temperature))
                self.assertIsInstance(self.ac.target_temperature, int)

    def test_invalid_temperature_cannot_change_state_or_send(self):
        self.ac._target_temp = 24
        for value in (25.5, "25.5", 15, 31, 0, -1, True, False, None,
                      float("nan"), float("inf"), float("-inf"), "unknown"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    asyncio.run(self.ac.async_set_temperature(temperature=value))
                self.assertEqual(self.ac.target_temperature, 24)
                self.assertEqual(self.ac.write_count, 0)
                self.assertEqual(self.hub.sent_analog, [])

    def test_missing_temperature_or_join_is_noop(self):
        asyncio.run(self.ac.async_set_temperature())
        self.ac._set_temp_join = None
        asyncio.run(self.ac.async_set_temperature(temperature=24))
        self.assertIsNone(self.ac.target_temperature)
        self.assertEqual(self.hub.sent_analog, [])

    def _restore(self, ac, target=24, room=23):
        async def last_state():
            return types.SimpleNamespace(state="off", attributes={
                "temperature": target, "current_temperature": room,
            })
        ac.async_get_last_state = last_state
        asyncio.run(ac.async_added_to_hass())

    def test_restore_valid_target_without_feedback(self):
        self._restore(self.ac, "24.0")
        self.assertEqual(self.ac.target_temperature, 24)
        self.assertEqual(self.ac.current_temperature, 23)

    def test_restore_rejects_invalid_target(self):
        for value in (25.5, 0, 31, True, "nan", "inf", "unknown"):
            with self.subTest(value=value):
                ac = make_ac(FakeHub())
                self._restore(ac, value)
                self.assertIsNone(ac.target_temperature)

    def test_live_zero_overrides_restored_values(self):
        self.hub.analog.update({414: 0, 415: 0})
        self._restore(self.ac)
        self.assertEqual(self.ac.target_temperature, 0)
        self.assertEqual(self.ac.current_temperature, 0)


class PowerOffTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.ac = make_ac(self.hub)

    def test_off_when_on_join_low_even_if_mode_latched(self):
        # CP4N reports a definitive off pair even though the mode remains latched.
        asyncio.run(self.ac.async_turn_off())
        self.ac._power_settle_until = 0.0  # settle window elapsed
        self.hub.digital[505] = False  # on_join feedback: powered off
        self.hub.digital[506] = True   # off_join feedback: powered off
        self.hub.digital[507] = True   # cool mode still latched high
        asyncio.run(self.ac.process_callback("d506", "1"))
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.OFF)

    def test_stale_on_join_within_settle_does_not_bounce(self):
        # Right after turn-off the control system may still be echoing on_join
        # high; within the settle window that stale level must be ignored.
        asyncio.run(self.ac.async_turn_off())
        self.hub.digital[505] = True  # stale "still on" feedback
        self.hub.digital[506] = False
        asyncio.run(self.ac.process_callback("d505", "1"))
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.OFF)

    def test_external_power_on_recognized_after_settle(self):
        # After the settle window, on_join going high (e.g. powered on at the
        # wall) is reflected as on, with the mode taken from the mode join.
        asyncio.run(self.ac.async_turn_off())
        self.ac._power_settle_until = 0.0  # settle window elapsed
        self.hub.digital[505] = True   # on_join feedback: powered on
        self.hub.digital[506] = False  # off_join feedback: powered on
        self.hub.digital[507] = True   # running in cool
        asyncio.run(self.ac.process_callback("d505", "1"))
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.COOL)

    def test_turn_off_leaves_mode_join_untouched(self):
        # Power off must NOT clear the mode joins (the unit should remember its
        # mode for the next power-on).
        asyncio.run(self.ac.async_set_hvac_mode(climate_mod.HVACMode.COOL))
        self.hub.sent_digital.clear()
        asyncio.run(self.ac.async_turn_off())
        self.assertNotIn((507, False), self.hub.sent_digital)

    def test_subscribes_to_both_power_feedback_joins(self):
        asyncio.run(self.ac.async_added_to_hass())
        self.assertIn("d505", self.hub.registered_joins)
        self.assertIn("d506", self.hub.registered_joins)

    def test_ambiguous_pair_keeps_optimistic_state(self):
        self.ac._optimistic_on = True
        self.ac._power_settle_until = 0.0
        self.hub.digital[505] = False
        self.hub.digital[506] = False
        asyncio.run(self.ac.process_callback("d505", "0"))
        self.assertNotEqual(self.ac.hvac_mode, climate_mod.HVACMode.OFF)


class DeviceGroupingTests(unittest.TestCase):
    """climate was the one platform that never built DeviceInfo, so ACs showed
    no device in HA even though the generator emits device_id/device_name."""

    def _ac(self, **extra):
        return climate_mod.CrestronAC(
            FakeHub(), {"name": "AC", "on_join": 505, "off_join": 506, **extra}
        )

    def test_device_id_builds_device_info(self):
        ac = self._ac(
            device_id="ac_505",
            device_name="B2.洗衣房 空调",
            suggested_area="B2.洗衣房",
        )
        self.assertEqual(
            ac._attr_device_info["identifiers"], {("crestron", "ac_505")}
        )
        self.assertEqual(ac._attr_device_info["name"], "B2.洗衣房 空调")
        self.assertEqual(
            ac._attr_device_info["suggested_area"], "B2.洗衣房"
        )

    def test_excel_name_keeps_identity_and_avoids_automatic_area_creation(self):
        ac = self._ac(name="风盘机", device_id="ac_505", device_name="风盘机",
                      floor="B2", room="洗衣房", suggested_area="B2.洗衣房")
        self.assertEqual(ac._attr_name, "风盘机")
        self.assertEqual(ac._attr_device_info["name"], "风盘机")
        self.assertEqual(ac._attr_device_info["identifiers"], {("crestron", "ac_505")})
        self.assertEqual(ac._attr_unique_id, "crestron_climate_d505")
        self.assertIsNone(ac._attr_device_info["suggested_area"])

    def test_device_name_defaults_to_id(self):
        ac = self._ac(device_id="ac_505")
        self.assertEqual(ac._attr_device_info["name"], "ac_505")

    def test_no_device_id_means_no_device(self):
        self.assertIsNone(self._ac()._attr_device_info)


class UniqueIdTests(unittest.TestCase):
    """Optional temperature feedback must never change climate identity."""

    def _ac(self, **joins):
        return climate_mod.CrestronAC(
            FakeHub(), {"name": "AC", "on_join": 505, "off_join": 506, **joins}
        )

    def test_id_uses_mandatory_on_join(self):
        self.assertEqual(
            self._ac(reg_temp_join=415)._attr_unique_id, "crestron_climate_d505"
        )
        self.assertEqual(
            self._ac(set_temp_join=414)._attr_unique_id, "crestron_climate_d505"
        )

    def test_optional_temperature_joins_do_not_change_id(self):
        ac = self._ac(reg_temp_join=415, set_temp_join=414)
        self.assertEqual(ac._attr_unique_id, self._ac()._attr_unique_id)

    def test_digital_fallback_is_namespaced(self):
        # No temperature joins at all -> falls back to the digital on_join.
        self.assertEqual(self._ac()._attr_unique_id, "crestron_climate_d505")

    def test_name_does_not_change_id(self):
        renamed = climate_mod.CrestronAC(
            FakeHub(),
            {"name": "Renamed AC", "on_join": 505, "off_join": 506},
        )
        self.assertEqual(renamed._attr_unique_id, self._ac()._attr_unique_id)


class ClimateFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.ac = climate_mod.CrestronAC(self.hub, {
            "name": "AC", "on_join": 505, "off_join": 506,
            "set_temp_join": 414, "reg_temp_join": 415,
            "mode_cool_join": 507, "mode_heat_join": 508,
            "mode_dry_join": 511, "fan_low_join": 512, "fan_high_join": 514,
        })
        self.hub.digital.update({505: True, 506: False, 507: True,
                                 508: False, 511: False, 512: True, 514: False})
        self.hub.analog.update({414: 16, 415: 24})
        await self.ac.async_added_to_hass()

    async def asyncTearDown(self):
        await self.ac.async_will_remove_from_hass()

    async def feedback(self, key, value):
        cache = self.hub.digital if key.startswith("d") else self.hub.analog
        cache[int(key[1:])] = value
        await self.ac.process_callback(key, str(int(value)))

    async def test_unrelated_updates_do_not_restore_old_target_or_fan(self):
        await self.ac.async_set_temperature(temperature=30)
        await self.ac.async_set_fan_mode("high")
        for key, value in (("d507", 1), ("d505", 1), ("a415", 25)):
            await self.feedback(key, value)
            self.assertEqual(self.ac.target_temperature, 30)
            self.assertEqual(self.ac.fan_mode, "high")
        # The shared hub is still an honest record of the old received values.
        self.assertEqual(self.hub.get_analog(414), 16)
        self.assertTrue(self.hub.get_digital(512))

    async def test_unrelated_updates_do_not_restore_old_mode(self):
        await self.ac.async_set_hvac_mode(HVACMode.HEAT)
        for key, value in (("a414", 16), ("a415", 25), ("d512", 1), ("d505", 1)):
            await self.feedback(key, value)
            self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)
        # Even a low report in the same group must not reselect cached cooling.
        await self.feedback("d511", 0)
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)
        self.assertTrue(self.hub.get_digital(507))

    async def test_fresh_target_feedback_can_change_30_back_to_15_or_zero(self):
        await self.ac.async_set_temperature(temperature=30)
        await self.feedback("a414", 30)
        await self.feedback("a414", 15)
        self.assertEqual(self.ac.target_temperature, 15)
        await self.feedback("a414", 0)
        self.assertEqual(self.ac.target_temperature, 0)

    async def test_fresh_mode_feedback_can_report_actual_return_to_cooling(self):
        await self.ac.async_set_hvac_mode(HVACMode.HEAT)
        await self.feedback("d508", 1)
        await self.feedback("d507", 0)
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)
        # Both high is transitional; wait until the controller clears heating.
        await self.feedback("d507", 1)
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)
        await self.feedback("d508", 0)
        self.assertEqual(self.ac.hvac_mode, HVACMode.COOL)

    async def test_fan_transition_does_not_reselect_old_low_level(self):
        await self.ac.async_set_fan_mode("high")
        await self.feedback("d514", 0)
        self.assertEqual(self.ac.fan_mode, "high")
        await self.feedback("d514", 1)
        await self.feedback("d512", 1)
        self.assertEqual(self.ac.fan_mode, "high")
        await self.feedback("d514", 0)
        self.assertEqual(self.ac.fan_mode, "low")

    async def test_power_command_does_not_reuse_old_on_cache_after_timeout(self):
        with patch.object(climate_mod, "POWER_SETTLE_SECONDS", 0.01):
            await self.ac.async_turn_off()
        # The 0.2 s pulse has already outlasted the timer. No power feedback
        # arrived, so a subsequent temperature report cannot resurrect old ON.
        await self.feedback("a414", 16)
        self.assertEqual(self.ac.hvac_mode, HVACMode.OFF)
        self.assertTrue(self.hub.get_digital(505))

    async def test_power_feedback_during_settle_is_applied_by_timer(self):
        with patch.object(climate_mod, "POWER_SETTLE_SECONDS", 0.3):
            await self.ac.async_turn_off()
        await self.feedback("d505", 1)
        self.assertEqual(self.ac.hvac_mode, HVACMode.OFF)
        await asyncio.sleep(0.15)
        self.assertEqual(self.ac.hvac_mode, HVACMode.COOL)

    async def test_reconnect_does_not_restore_stale_fields(self):
        await self.ac.async_set_temperature(temperature=30)
        await self.ac.async_set_hvac_mode(HVACMode.HEAT)
        await self.ac.async_set_fan_mode("high")
        await self.ac.process_callback(climate_mod.AVAILABLE_KEY, "False")
        await self.ac.process_callback(climate_mod.AVAILABLE_KEY, "True")
        self.assertEqual(self.ac.target_temperature, 30)
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)
        self.assertEqual(self.ac.fan_mode, "high")
        await self.feedback("d511", 0)
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)

    async def test_remove_cancels_pending_power_timer(self):
        with patch.object(climate_mod, "POWER_SETTLE_SECONDS", 0.3):
            await self.ac.async_turn_off()
        await self.feedback("d505", 1)
        writes = self.ac.write_count
        await self.ac.async_will_remove_from_hass()
        await asyncio.sleep(0.15)
        self.assertEqual(self.ac.write_count, writes)
        self.assertEqual(self.ac.hvac_mode, HVACMode.OFF)

    async def test_replacing_power_command_cancels_previous_timer(self):
        with patch.object(climate_mod, "POWER_SETTLE_SECONDS", 0.5):
            await self.ac.async_turn_off()
            previous = self.ac._power_settle_handle
            await self.feedback("d506", 1)
            await self.ac.async_turn_on()
        self.assertTrue(previous.cancelled())
        self.assertEqual(self.ac.hvac_mode, HVACMode.COOL)
        # The old command's OFF feedback was invalidated by the new command.
        await asyncio.sleep(0.35)
        self.assertEqual(self.ac.hvac_mode, HVACMode.COOL)


class ClimateCommandOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.ac = climate_mod.CrestronAC(self.hub, {
            "name": "AC", "on_join": 505, "off_join": 506,
            "set_temp_join": 414, "mode_cool_join": 507, "mode_heat_join": 508,
            "fan_low_join": 512, "fan_high_join": 514,
        })

    async def asyncTearDown(self):
        await self.ac.async_will_remove_from_hass()

    async def test_later_heating_cannot_be_overwritten_by_suspended_cooling(self):
        cooling = asyncio.create_task(self.ac.async_set_hvac_mode(HVACMode.COOL))
        await asyncio.sleep(0)  # first command is holding its power-on pulse
        await self.ac.async_set_hvac_mode(HVACMode.HEAT)
        await cooling
        selected = [(j, v) for j, v in self.hub.sent_digital
                    if j in (507, 508) and v]
        self.assertEqual(selected, [(507, True), (508, True)])
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)

    async def test_off_waits_for_whole_mode_command(self):
        heating = asyncio.create_task(self.ac.async_set_hvac_mode(HVACMode.HEAT))
        await asyncio.sleep(0)
        await self.ac.async_turn_off()
        await heating
        self.assertEqual(self.hub.sent_digital[-2:], [(506, True), (506, False)])
        self.assertEqual(self.ac.hvac_mode, HVACMode.OFF)

    async def test_latest_heating_after_queued_off_sends_power_on_again(self):
        first = asyncio.create_task(self.ac.async_set_hvac_mode(HVACMode.COOL))
        await asyncio.sleep(0)
        off = asyncio.create_task(self.ac.async_turn_off())
        await asyncio.sleep(0)
        await self.ac.async_set_hvac_mode(HVACMode.HEAT)
        await asyncio.gather(first, off)
        self.assertEqual(self.hub.sent_digital[-4:],
                         [(505, True), (505, False), (507, False), (508, True)])
        self.assertEqual(self.ac.hvac_mode, HVACMode.HEAT)

    async def test_temperature_and_fan_wait_for_inflight_power_command(self):
        turn_on = asyncio.create_task(self.ac.async_turn_on())
        await asyncio.sleep(0)
        temp = asyncio.create_task(self.ac.async_set_temperature(temperature=30))
        fan = asyncio.create_task(self.ac.async_set_fan_mode("high"))
        await asyncio.sleep(0)
        self.assertEqual(self.hub.sent_analog, [])
        self.assertNotIn((514, True), self.hub.sent_digital)
        await asyncio.gather(turn_on, temp, fan)
        self.assertEqual(self.hub.sent_analog, [(414, 30)])
        self.assertEqual(self.ac.target_temperature, 30)
        self.assertEqual(self.ac.fan_mode, "high")

    async def test_cancelled_mode_releases_pulse_and_allows_next_command(self):
        first = asyncio.create_task(self.ac.async_set_hvac_mode(HVACMode.COOL))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.hub.sent_digital, [(505, True), (505, False)])
        await asyncio.wait_for(self.ac.async_turn_off(), timeout=1)
        self.assertEqual(self.hub.sent_digital[-2:], [(506, True), (506, False)])
        self.assertEqual(self.ac.hvac_mode, HVACMode.OFF)

    async def test_removal_drops_queued_commands_but_releases_active_pulse(self):
        first = asyncio.create_task(self.ac.async_set_hvac_mode(HVACMode.COOL))
        await asyncio.sleep(0)
        queued = asyncio.create_task(self.ac.async_set_temperature(temperature=30))
        await asyncio.sleep(0)
        await self.ac.async_will_remove_from_hass()
        await asyncio.gather(first, queued)
        self.assertEqual(self.hub.sent_digital, [(505, True), (505, False)])
        self.assertEqual(self.hub.sent_analog, [])

    async def test_entities_do_not_share_a_global_command_lock(self):
        other = climate_mod.CrestronAC(self.hub, {
            "name": "Other", "on_join": 595, "off_join": 596,
        })
        first = asyncio.create_task(self.ac.async_turn_off())
        second = asyncio.create_task(other.async_turn_off())
        await asyncio.sleep(0)
        self.assertEqual(self.hub.sent_digital, [(506, True), (596, True)])
        await asyncio.gather(first, second)
        await other.async_will_remove_from_hass()


if __name__ == "__main__":
    unittest.main()
