"""Tests for sensor / binary_sensor "not reported yet" handling.

The control system pushes joins on change only, so a join it has never sent is
*unknown* — distinct from a real 0 / False. Reporting the zero value instead is
not harmless: a sensor carrying `state_class` writes those fake datapoints into
long-term statistics, and they stay in the recorder database.

sensor.py / binary_sensor.py import homeassistant + voluptuous, neither
installed in the bare test environment, so minimal stand-ins are installed into
sys.modules before loading them via the synthetic-package loader (same approach
as test_cover_position).
"""

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


class SensorEntity:
    pass


class BinarySensorEntity:
    pass


class _Invalid(Exception):
    pass


def _passthrough_schema(*a, **k):
    # Both modules use vol.* only at import time to build PLATFORM_SCHEMA; the
    # tests construct entities directly, so these just need to not blow up.
    def _factory(*args, **kwargs):
        return lambda x: x

    return _factory


def _install_ha_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    components = _module("homeassistant.components")
    sensor = _module(
        "homeassistant.components.sensor",
        SensorEntity=SensorEntity,
        CONF_STATE_CLASS="state_class",
    )
    binary_sensor = _module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=BinarySensorEntity,
    )
    _module(
        "homeassistant.const",
        CONF_NAME="name",
        CONF_DEVICE_CLASS="device_class",
        CONF_UNIT_OF_MEASUREMENT="unit_of_measurement",
    )
    ha.helpers = helpers
    ha.components = components
    components.sensor = sensor
    components.binary_sensor = binary_sensor


def _fake_voluptuous():
    vol = types.ModuleType("voluptuous")
    vol.Schema = _passthrough_schema()
    vol.All = _passthrough_schema()
    vol.Optional = lambda *a, **k: ("opt", a)
    vol.Required = lambda *a, **k: ("req", a)
    vol.Range = lambda *a, **k: (lambda x: x)
    vol.Coerce = lambda *a, **k: (lambda x: x)
    vol.Invalid = _Invalid
    vol.ALLOW_EXTRA = 1
    return vol


_install_ha_stubs()

# Same voluptuous contention dance as test_cover_position: prefer the real
# library when installed (CI), fake it only in a bare env, then undo the
# pollution so test_schema still sees a clean state.
try:
    import voluptuous  # noqa: F401

    _faked_vol = False
except ImportError:
    sys.modules["voluptuous"] = _fake_voluptuous()
    _faked_vol = True

_schema_key = "crestron_under_test.schema"
_prev_schema = sys.modules.get(_schema_key)

sensor_mod = load("sensor")
binary_sensor_mod = load("binary_sensor")

if _faked_vol:
    sys.modules.pop("voluptuous", None)
    if _prev_schema is None:
        sys.modules.pop(_schema_key, None)
    else:
        sys.modules[_schema_key] = _prev_schema


class FakeHub:
    """Only reports what has actually been pushed, like the real hub."""

    def __init__(self):
        self.analog = {}
        self.digital = {}

    def get_analog(self, join, default=0):
        return self.analog.get(join, default)

    def has_analog(self, join):
        return join in self.analog

    def get_digital(self, join, default=False):
        return self.digital.get(join, default)

    def has_digital(self, join):
        return join in self.digital

    def is_available(self):
        return True

    def register_callback(self, cb, joins=None):
        pass

    def remove_callback(self, cb):
        pass


class AnalogSensorTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.sensor = sensor_mod.CrestronSensor(
            self.hub,
            {"name": "室外温度", "value_join": 1, "divisor": 10,
             "state_class": "measurement"},
        )

    def test_unknown_before_first_report(self):
        # Must be None, not 0.0 — a fake 0 here lands in long-term statistics.
        self.assertIsNone(self.sensor.native_value)

    def test_reports_value_after_push(self):
        self.hub.analog[1] = 235
        self.assertEqual(self.sensor.native_value, 23.5)

    def test_real_zero_is_reported(self):
        # A genuine 0 that the control system actually sent is a real reading
        # and must not be swallowed along with "never reported".
        self.hub.analog[1] = 0
        self.assertEqual(self.sensor.native_value, 0)


class ModeSensorTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.sensor = sensor_mod.CrestronSensor(
            self.hub,
            {"name": "运行模式", "mode_joins": {"制冷": 507, "制热": 508}},
        )

    def test_unknown_before_first_report(self):
        self.assertIsNone(self.sensor.native_value)

    def test_all_low_after_report_is_off(self):
        self.hub.digital[507] = False
        self.hub.digital[508] = False
        self.assertEqual(self.sensor.native_value, sensor_mod.MODE_OFF)

    def test_active_label_wins(self):
        self.hub.digital[507] = False
        self.hub.digital[508] = True
        self.assertEqual(self.sensor.native_value, "制热")

    def test_partial_report_is_not_enough_to_decide_off(self):
        """"Off" needs every mode join low, so every one must be reported.

        The joins arrive one frame at a time during the initial sync. Deciding
        "关闭" from the first low join made the sensor claim off while the join
        that was actually high simply hadn't landed yet.
        """
        self.hub.digital[507] = False
        self.assertIsNone(self.sensor.native_value)

    def test_partial_report_still_reports_an_active_mode(self):
        """A high join is definitive on its own — no need to wait."""
        self.hub.digital[508] = True
        self.assertEqual(self.sensor.native_value, "制热")


class BinarySensorTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.sensor = binary_sensor_mod.CrestronBinarySensor(
            self.hub, {"name": "空压机", "is_on_join": 57}
        )

    def test_unknown_before_first_report(self):
        # None -> HA shows "unknown"; returning False would assert an "off"
        # state that automations act on but nobody ever told us.
        self.assertIsNone(self.sensor.is_on)

    def test_reports_true_after_push(self):
        self.hub.digital[57] = True
        self.assertTrue(self.sensor.is_on)

    def test_reports_real_false(self):
        self.hub.digital[57] = False
        self.assertIs(self.sensor.is_on, False)


if __name__ == "__main__":
    unittest.main()
