"""Platform schemas must reject configs that build uncontrollable entities.

Every join is individually optional, so without cross-field validation a
hand-written entry could pass validation and then produce an entity with no way
to command it (and a unique_id ending in "None"). The xlsx converter never
emits these, but hand-written YAML and `crestron.reload` both can.
"""

import asyncio
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

    def test_fractional_values_rejected(self):
        for key in ("min", "max", "step"):
            with self.assertRaises(vol.Invalid):
                number.PLATFORM_SCHEMA(
                    {"name": "n", "value_join": 1, key: 0.5}
                )


class NonFiniteTests(unittest.TestCase):
    """nan compares False against everything, so it slips past range checks."""

    def test_number_rejects_nan_and_inf(self):
        for key in ("min", "max", "step"):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.assertRaises(vol.Invalid):
                    number.PLATFORM_SCHEMA(
                        {"name": "n", "value_join": 1, key: bad}
                    )

    def test_sensor_divisor_must_be_finite_and_positive(self):
        base = {"name": "s", "value_join": 1}
        # A negative divisor would silently invert every reading's sign.
        for bad in (0, -10, float("nan"), float("inf")):
            with self.assertRaises(vol.Invalid):
                sensor.PLATFORM_SCHEMA({**base, "divisor": bad})

    def test_sensor_divisor_normal_values_accepted(self):
        base = {"name": "s", "value_join": 1}
        self.assertEqual(sensor.PLATFORM_SCHEMA({**base, "divisor": 10})["divisor"], 10.0)
        self.assertEqual(sensor.PLATFORM_SCHEMA(base)["divisor"], 1.0)


class MediaSourceNumberTests(unittest.TestCase):
    BASE = {
        "name": "音箱", "mute_join": 27, "volume_join": 19,
        "source_number_join": 13,
    }

    def test_source_zero_rejected(self):
        """0 is this component's "off" value: such a source can never turn on."""
        with self.assertRaises(vol.Invalid):
            media_player.PLATFORM_SCHEMA({**self.BASE, "sources": {0: "HDMI"}})
        media_player.PLATFORM_SCHEMA({**self.BASE, "sources": {1: "HDMI"}})

    def test_bool_and_float_source_numbers_rejected(self):
        """Coerce(int) mapped True->1 and 1.9->1, colliding with a real source."""
        for bad in (True, False, 1.9, "1.9", "abc"):
            with self.assertRaises(vol.Invalid):
                media_player.PLATFORM_SCHEMA(
                    {**self.BASE, "sources": {bad: "HDMI"}}
                )

    def test_decimal_string_source_number_accepted(self):
        """YAML quoting is easy to do by accident."""
        got = media_player.PLATFORM_SCHEMA(
            {**self.BASE, "sources": {"2": "Roku"}}
        )["sources"]
        self.assertEqual(got, {2: "Roku"})

    def test_unicode_digits_rejected_as_vol_invalid(self):
        with self.assertRaises(vol.Invalid):
            media_player.PLATFORM_SCHEMA(
                {**self.BASE, "sources": {"²": "Roku"}}
            )

    def test_normalized_source_numbers_must_be_unique(self):
        with self.assertRaises(vol.Invalid):
            media_player.PLATFORM_SCHEMA(
                {**self.BASE, "sources": {"1": "TV", 1: "Roku"}}
            )

    def test_source_display_names_must_be_unique(self):
        with self.assertRaises(vol.Invalid):
            media_player.PLATFORM_SCHEMA(
                {**self.BASE, "sources": {1: "TV", 2: "TV"}}
            )


class NumberBehaviourTests(unittest.TestCase):
    class Hub:
        def __init__(self):
            self.analog = {}
            self.sent = []

        def is_available(self):
            return True

        def has_analog(self, join):
            return join in self.analog

        def get_analog(self, join):
            return self.analog.get(join, 0)

        def set_analog(self, join, value):
            self.sent.append((join, value))

        def register_callback(self, callback, joins):
            pass

        def remove_callback(self, callback):
            pass

    def _entity(self, **overrides):
        config = number.PLATFORM_SCHEMA(
            {
                "name": "设定值",
                "value_join": 1,
                "min": 0,
                "max": 30,
                **overrides,
            }
        )
        hub = self.Hub()
        entity = number.CrestronNumber(hub, config)
        entity.async_write_ha_state = lambda: None
        return hub, entity

    def test_reported_zero_is_not_ignored(self):
        hub, entity = self._entity()
        hub.analog[1] = 0

        async def no_last_state():
            return None

        entity.async_get_last_state = no_last_state
        asyncio.run(entity.async_added_to_hass())
        self.assertEqual(entity.native_value, 0)

    def test_restore_ignores_value_outside_configured_range(self):
        hub, entity = self._entity(min=16)

        async def old_out_of_range_state():
            return types.SimpleNamespace(state="0")

        entity.async_get_last_state = old_out_of_range_state
        asyncio.run(entity.async_added_to_hass())
        self.assertIsNone(entity.native_value)

    def test_fractional_command_is_rejected_without_writing(self):
        hub, entity = self._entity()
        with self.assertRaises(ValueError):
            asyncio.run(entity.async_set_native_value(20.5))
        self.assertEqual(hub.sent, [])
        self.assertIsNone(entity.native_value)


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
