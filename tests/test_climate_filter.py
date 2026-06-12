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
        pass

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
        # The core fix: power is read from on_join. After turn-off, on_join goes
        # low — the unit is OFF even though the cool mode join is still latched.
        asyncio.run(self.ac.async_turn_off())
        self.ac._power_settle_until = 0.0  # settle window elapsed
        self.hub.digital[505] = False  # on_join feedback: powered off
        self.hub.digital[507] = True   # cool mode still latched high
        asyncio.run(self.ac.process_callback("d505", "0"))
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.OFF)

    def test_stale_on_join_within_settle_does_not_bounce(self):
        # Right after turn-off the control system may still be echoing on_join
        # high; within the settle window that stale level must be ignored.
        asyncio.run(self.ac.async_turn_off())
        self.hub.digital[505] = True  # stale "still on" feedback
        asyncio.run(self.ac.process_callback("d505", "1"))
        self.assertEqual(self.ac.hvac_mode, climate_mod.HVACMode.OFF)

    def test_external_power_on_recognized_after_settle(self):
        # After the settle window, on_join going high (e.g. powered on at the
        # wall) is reflected as on, with the mode taken from the mode join.
        asyncio.run(self.ac.async_turn_off())
        self.ac._power_settle_until = 0.0  # settle window elapsed
        self.hub.digital[505] = True   # on_join feedback: powered on
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


if __name__ == "__main__":
    unittest.main()
