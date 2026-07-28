"""Platform schemas must reject configs that build uncontrollable entities.

Every join is individually optional, so without cross-field validation a
hand-written entry could pass validation and then produce an entity with no way
to command it (and a unique_id ending in "None"). The xlsx converter never
emits these, but hand-written YAML and `crestron.reload` both can.
"""

import sys
import types
import unittest

import voluptuous as vol

from loader import load


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _Enum(str):
    def __new__(cls, value):
        return str.__new__(cls, value)


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.config_validation",
        string=str,
        positive_int=int,
        entity_id=str,
    )
    _module(
        "homeassistant.helpers.restore_state",
        RestoreEntity=type("RestoreEntity", (), {}),
    )
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    _module(
        "homeassistant.const",
        CONF_NAME="name",
        CONF_TYPE="type",
        CONF_DEVICE_CLASS="device_class",
        CONF_UNIT_OF_MEASUREMENT="unit_of_measurement",
        ATTR_TEMPERATURE="temperature",
    )

    class ColorMode:
        ONOFF = "onoff"
        BRIGHTNESS = "brightness"
        COLOR_TEMP = "color_temp"

    _module(
        "homeassistant.components.light", ColorMode=ColorMode, LightEntity=type("LightEntity", (), {})
    )

    class _DC:
        def __getattr__(self, item):
            return item.lower()

    _module("homeassistant.components.select", SelectEntity=type("SelectEntity", (), {}))
    _module(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        CONF_STATE_CLASS="state_class",
    )
    _module("homeassistant.components.number", NumberEntity=type("NumberEntity", (), {}))
    _module(
        "homeassistant.components.media_player",
        MediaPlayerEntity=type("MediaPlayerEntity", (), {}),
        MediaPlayerEntityFeature=type("F", (), {"__getattr__": lambda s, i: 1})(),
        MediaPlayerDeviceClass=_DC(),
        MediaPlayerState=_DC(),
    )
    ha.helpers = helpers


_install_stubs()
light = load("light")
sensor = load("sensor")
select = load("select")
number = load("number")
media_player = load("media_player")
schema = load("schema")


class LightCapabilityTests(unittest.TestCase):
    def _valid(self, cfg):
        return light.PLATFORM_SCHEMA(cfg)

    def _invalid(self, cfg):
        with self.assertRaises(vol.Invalid):
            light.PLATFORM_SCHEMA(cfg)

    # --- shapes that exist in the real project config: must keep working ---
    def test_dimmable_with_color_temp_accepted(self):
        self._valid({"name": "灯", "brightness_join": 1, "color_temp_join": 201})

    def test_dimmable_only_accepted(self):
        self._valid({"name": "灯", "brightness_join": 2})

    def test_pulse_pair_accepted(self):
        self._valid({"name": "灯", "on_join": 1, "off_join": 2})

    def test_switch_join_accepted(self):
        self._valid({"name": "灯", "switch_join": 65})

    def test_pulse_pair_with_state_join_accepted(self):
        self._valid({"name": "灯", "on_join": 1, "off_join": 2, "state_join": 50})

    # --- shapes that used to build broken entities ---
    def test_color_temp_without_brightness_rejected(self):
        self._invalid({"name": "灯", "color_temp_join": 201})

    def test_half_a_pulse_pair_rejected(self):
        self._invalid({"name": "灯", "on_join": 1})
        self._invalid({"name": "灯", "off_join": 2})

    def test_no_control_join_rejected(self):
        self._invalid({"name": "灯"})

    def test_feedback_only_rejected(self):
        """state_join alone yields crestron_light_onoff_None and no control."""
        self._invalid({"name": "灯", "state_join": 50})

    def test_mixed_analog_and_digital_control_rejected(self):
        self._invalid(
            {"name": "灯", "brightness_join": 1, "on_join": 2, "off_join": 3}
        )


class EmptyCollectionTests(unittest.TestCase):
    def test_empty_mode_joins_rejected(self):
        with self.assertRaises(vol.Invalid):
            sensor.PLATFORM_SCHEMA({"name": "s", "mode_joins": {}})

    def test_sensor_still_requires_exactly_one_source(self):
        with self.assertRaises(vol.Invalid):
            sensor.PLATFORM_SCHEMA({"name": "s"})
        with self.assertRaises(vol.Invalid):
            sensor.PLATFORM_SCHEMA(
                {"name": "s", "value_join": 1, "mode_joins": {"a": 2}}
            )
        sensor.PLATFORM_SCHEMA({"name": "s", "value_join": 1})
        sensor.PLATFORM_SCHEMA({"name": "s", "mode_joins": {"制冷": 507}})

    def test_empty_select_options_rejected(self):
        with self.assertRaises(vol.Invalid):
            select.PLATFORM_SCHEMA({"name": "风速", "options": {}})
        select.PLATFORM_SCHEMA({"name": "风速", "options": {"低": 512}})

    def test_empty_media_sources_rejected(self):
        base = {
            "name": "音箱", "mute_join": 27, "volume_join": 19,
            "source_number_join": 13,
        }
        with self.assertRaises(vol.Invalid):
            media_player.PLATFORM_SCHEMA({**base, "sources": {}})
        media_player.PLATFORM_SCHEMA({**base, "sources": {1: "TV"}})


class NumberRangeTests(unittest.TestCase):
    def test_min_must_be_below_max(self):
        with self.assertRaises(vol.Invalid):
            number.PLATFORM_SCHEMA(
                {"name": "n", "value_join": 1, "min": 30, "max": 16}
            )
        with self.assertRaises(vol.Invalid):
            number.PLATFORM_SCHEMA(
                {"name": "n", "value_join": 1, "min": 20, "max": 20}
            )

    def test_step_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(vol.Invalid):
                number.PLATFORM_SCHEMA(
                    {"name": "n", "value_join": 1, "step": bad}
                )

    def test_defaults_are_usable(self):
        cfg = number.PLATFORM_SCHEMA({"name": "n", "value_join": 1})
        self.assertLess(cfg["min"], cfg["max"])
        self.assertGreater(cfg["step"], 0)


class JoinNumberTests(unittest.TestCase):
    def test_bool_rejected(self):
        """bool is a subclass of int; `on_join: true` became the key "dTrue"."""
        for validator in (schema.digital_join, schema.analog_join):
            with self.assertRaises(vol.Invalid):
                validator(True)
            with self.assertRaises(vol.Invalid):
                validator(False)

    def test_non_int_rejected(self):
        for bad in (3.7, "5", None, [1]):
            with self.assertRaises(vol.Invalid):
                schema.digital_join(bad)

    def test_range_enforced(self):
        self.assertEqual(schema.digital_join(4096), 4096)
        with self.assertRaises(vol.Invalid):
            schema.digital_join(4097)
        with self.assertRaises(vol.Invalid):
            schema.digital_join(0)
        self.assertEqual(schema.analog_join(1024), 1024)
        with self.assertRaises(vol.Invalid):
            schema.analog_join(1025)


if __name__ == "__main__":
    unittest.main()
