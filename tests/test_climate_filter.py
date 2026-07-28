"""Tests for the climate room-temp report threshold (TEMP_REPORT_THRESHOLD).

climate.py imports homeassistant, which isn't installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before
loading the module via the synthetic-package loader. voluptuous is real.
"""

import asyncio
import enum
import sys
import types
import unittest

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

    def get_analog(self, join):
        return self.analog.get(join, 0)

    def get_digital(self, join):
        return self.digital.get(join, False)

    def set_digital(self, join, value):
        self.sent_digital.append((join, bool(value)))

    def set_analog(self, join, value):
        pass

    def is_available(self):
        return True

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

    def test_unknown_zero_dropped(self):
        self._temp_event(0)
        self.assertIsNone(self.ac.current_temperature)
        self.assertEqual(self.ac.write_count, 0)

    def test_other_events_still_write_and_refresh_temp(self):
        self._temp_event(24)
        # temp drifts below threshold, then a mode join fires: the mode event
        # writes state anyway and opportunistically refreshes the cached temp.
        # Power is read from on_join, so assert it high to land in COOL.
        self.hub.analog[415] = 24.3
        self.hub.digital[505] = True  # on_join feedback: powered on
        self.hub.digital[507] = True  # running in cool
        asyncio.run(self.ac.process_callback("d507", "1"))
        self.assertEqual(self.ac.write_count, 2)
        self.assertEqual(self.ac.current_temperature, 24.3)
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.COOL)


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


if __name__ == "__main__":
    unittest.main()
