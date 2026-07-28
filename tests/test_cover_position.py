"""Tests for the cover's 0–100 position scaling (read + write).

cover.py imports homeassistant + voluptuous, neither installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before loading
the module via the synthetic-package loader (same approach as
test_onoff_light / test_climate_filter).

Position is a 0–100 analog (0=closed, 100=open) reported directly by the
control system — NOT the XSIG 0–65535 full scale — so the entity must read and
write it verbatim.
"""

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


class CoverDeviceClass(str, enum.Enum):
    AWNING = "awning"
    BLIND = "blind"
    CURTAIN = "curtain"
    DAMPER = "damper"
    DOOR = "door"
    GARAGE = "garage"
    GATE = "gate"
    SHADE = "shade"
    SHUTTER = "shutter"
    WINDOW = "window"


class CoverEntityFeature(enum.IntFlag):
    OPEN = 1
    CLOSE = 2
    STOP = 4
    SET_POSITION = 8


class CoverEntity:
    def async_write_ha_state(self):
        pass


class RestoreEntity:
    _last_state = None

    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return self._last_state


class _Invalid(Exception):
    pass


def _passthrough_schema(*a, **k):
    # cover.py uses vol.Schema/vol.All/vol.Required/... only at import time to
    # build PLATFORM_SCHEMA; the tests construct entities directly, so these
    # just need to exist and not blow up at module load.
    def _factory(*args, **kwargs):
        return lambda x: x
    return _factory


def _install_ha_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.entity", DeviceInfo=dict)
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    components = _module("homeassistant.components")
    cover = _module(
        "homeassistant.components.cover",
        CoverDeviceClass=CoverDeviceClass,
        CoverEntity=CoverEntity,
        CoverEntityFeature=CoverEntityFeature,
    )
    _module("homeassistant.const", CONF_NAME="name", CONF_TYPE="type")
    ha.helpers = helpers
    ha.components = components
    components.cover = cover


def _fake_voluptuous():
    # Minimal voluptuous surface used at cover.py/schema.py import time.
    vol = types.ModuleType("voluptuous")
    vol.Schema = _passthrough_schema()
    vol.All = _passthrough_schema()
    vol.Optional = lambda *a, **k: ("opt", a)
    vol.Required = lambda *a, **k: ("req", a)
    vol.Range = lambda *a, **k: (lambda x: x)
    vol.Invalid = _Invalid
    vol.ALLOW_EXTRA = 1
    return vol


_install_ha_stubs()

# voluptuous is contended: test_schema needs the *real* one (on CI). If it's
# installed, use it; only fall back to a fake when absent (bare env), and then
# undo the pollution immediately after loading cover so test_schema still sees
# a clean state (real vol on CI, or nothing -> it self-skips in bare env).
try:
    import voluptuous  # noqa: F401

    _faked_vol = False
except ImportError:
    sys.modules["voluptuous"] = _fake_voluptuous()
    _faked_vol = True

# Snapshot so we can restore the schema-module cache too (it captures whichever
# voluptuous was live at its import).
_schema_key = "crestron_under_test.schema"
_prev_schema = sys.modules.get(_schema_key)

cover_mod = load("cover")

if _faked_vol:
    # Remove the fake and the fake-built schema so later test modules reload
    # cleanly (cover_mod keeps its own captured references — harmless for our
    # tests, which don't exercise the schema through cover).
    sys.modules.pop("voluptuous", None)
    if _prev_schema is None:
        sys.modules.pop(_schema_key, None)
    else:
        sys.modules[_schema_key] = _prev_schema


class FakeHub:
    def __init__(self):
        self.analog = {}
        self.digital = {}
        self.sent_analog = []  # (join, value) analog writes to the wire
        self.sent_digital = []  # (join, value) digital writes to the wire

    def get_analog(self, join, default=0):
        return self.analog.get(join, default)

    def has_analog(self, join):
        return join in self.analog

    def get_digital(self, join, default=False):
        return self.digital.get(join, default)

    def set_analog(self, join, value):
        self.sent_analog.append((join, value))

    def set_digital(self, join, value):
        self.sent_digital.append((join, bool(value)))

    def is_available(self):
        return True

    def register_callback(self, cb, joins=None):
        self.registered_joins = joins

    def remove_callback(self, cb):
        pass


def make_cover(hub):
    """Cover with a real position feedback join (pos_join)."""
    return cover_mod.CrestronShade(
        hub,
        {"name": "客厅纱帘", "type": "curtain",
         "pos_join": 480, "stop_join": 702},
    )


def make_optimistic_cover(hub, last_state=None):
    """Cover whose position is inferred from open/close commands.

    Real Crestron scenario: pos_join is configured but the control system never
    reports it, and open/close/stop are momentary command joins.
    """
    ent = cover_mod.CrestronShade(
        hub,
        {"name": "客厅布帘", "type": "curtain", "pos_join": 481,
         "open_join": 704, "close_join": 705, "stop_join": 706},
    )
    if last_state is not None:
        ent._last_state = last_state
    return ent


class CoverTypeTests(unittest.TestCase):
    def _cover(self, cover_type=None):
        config = {
            "name": "测试",
            "pos_join": 480,
            "stop_join": 702,
        }
        if cover_type is not None:
            config["type"] = cover_type
        return cover_mod.CrestronShade(FakeHub(), config)

    def test_all_supported_types_map_to_matching_device_class(self):
        for cover_type in (
            "awning", "blind", "curtain", "damper", "door",
            "garage", "gate", "shade", "shutter", "window",
        ):
            with self.subTest(cover_type=cover_type):
                self.assertEqual(
                    self._cover(cover_type)._attr_device_class.value,
                    cover_type,
                )

    def test_type_is_trimmed_and_case_insensitive(self):
        self.assertEqual(
            self._cover(" Shade ")._attr_device_class,
            CoverDeviceClass.SHADE,
        )

    def test_missing_blank_or_unknown_type_defaults_to_curtain(self):
        self.assertEqual(
            self._cover()._attr_device_class,
            CoverDeviceClass.CURTAIN,
        )
        for cover_type in ("", "unsupported"):
            with self.subTest(cover_type=cover_type):
                self.assertEqual(
                    self._cover(cover_type)._attr_device_class,
                    CoverDeviceClass.CURTAIN,
                )


class UniqueIdTests(unittest.TestCase):
    """pos_join is analog, open/close_join are digital — separate join spaces.

    Deriving the id from "first one configured" without recording which space
    it came from lets two covers collide, and HA drops the one registered
    second.
    """

    def _cover(self, **joins):
        return cover_mod.CrestronShade(
            FakeHub(), {"name": "x", "type": "curtain", "stop_join": 702, **joins}
        )

    def test_pos_only_fallback_keeps_bare_number(self):
        self.assertEqual(
            self._cover(pos_join=480)._attr_unique_id, "crestron_cover_480"
        )

    def test_digital_fallback_is_namespaced(self):
        self.assertEqual(
            self._cover(open_join=704, close_join=705)._attr_unique_id,
            "crestron_cover_d704",
        )

    def test_analog_and_digital_same_number_do_not_collide(self):
        self.assertNotEqual(
            self._cover(pos_join=480)._attr_unique_id,
            self._cover(open_join=480, close_join=481)._attr_unique_id,
        )

    def test_optional_position_join_does_not_change_id(self):
        without = self._cover(open_join=704, close_join=705)._attr_unique_id
        with_position = self._cover(
            open_join=704, close_join=705, pos_join=480
        )._attr_unique_id
        self.assertEqual(without, "crestron_cover_d704")
        self.assertEqual(with_position, without)


class CoverPositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.cover = make_cover(self.hub)

    def test_position_unknown_before_report(self):
        self.assertIsNone(self.cover.current_cover_position)
        self.assertIsNone(self.cover.is_closed)

    def test_position_read_verbatim(self):
        # 50 means 50% — not 50/65535.
        self.hub.analog[480] = 50
        self.assertEqual(self.cover.current_cover_position, 50)
        self.assertFalse(self.cover.is_closed)

    def test_position_zero_is_closed(self):
        self.hub.analog[480] = 0
        self.assertEqual(self.cover.current_cover_position, 0)
        self.assertTrue(self.cover.is_closed)

    def test_position_full_open(self):
        self.hub.analog[480] = 100
        self.assertEqual(self.cover.current_cover_position, 100)

    def test_out_of_range_clamped(self):
        self.hub.analog[480] = 250
        self.assertEqual(self.cover.current_cover_position, 100)

    def test_slider_offered_with_real_feedback(self):
        from homeassistant.components.cover import CoverEntityFeature
        self.hub.analog[480] = 30
        self.assertTrue(
            self.cover.supported_features & CoverEntityFeature.SET_POSITION
        )
        self.assertFalse(self.cover.assumed_state)

    async def test_set_position_writes_verbatim(self):
        await self.cover.async_set_cover_position(position=37)
        self.assertEqual(self.hub.sent_analog, [(480, 37)])


class CoverOptimisticFallbackTests(unittest.IsolatedAsyncioTestCase):
    """No real position feedback: infer position from open/close commands."""

    def setUp(self):
        self.hub = FakeHub()
        self.cover = make_optimistic_cover(self.hub)

    def test_unknown_before_any_command(self):
        self.assertIsNone(self.cover.current_cover_position)

    def test_slider_is_stable_before_feedback(self):
        from homeassistant.components.cover import CoverEntityFeature
        self.assertTrue(
            self.cover.supported_features & CoverEntityFeature.SET_POSITION
        )
        # assumed_state so the open/close buttons aren't gated on state.
        self.assertTrue(self.cover.assumed_state)

    async def test_set_position_is_optimistic_before_feedback(self):
        await self.cover.async_set_cover_position(position=37)
        self.assertEqual(self.cover.current_cover_position, 37)
        self.assertEqual(self.hub.sent_analog, [(481, 37)])

    async def test_open_close_feedback_reconciles_state(self):
        await self.cover.async_added_to_hass()
        self.assertIn("d704", self.hub.registered_joins)
        self.assertIn("d705", self.hub.registered_joins)

        self.hub.digital[704] = True
        self.hub.digital[705] = False
        await self.cover.process_callback("d704", "1")
        self.assertEqual(self.cover.current_cover_position, 100)
        self.assertFalse(self.cover.is_closed)

        self.hub.digital[704] = False
        self.hub.digital[705] = True
        await self.cover.process_callback("d705", "1")
        self.assertEqual(self.cover.current_cover_position, 0)
        self.assertTrue(self.cover.is_closed)

    async def test_open_infers_full_open(self):
        await self.cover.async_open_cover()
        self.assertEqual(self.cover.current_cover_position, 100)
        self.assertFalse(self.cover.is_closed)
        self.assertIn((704, True), self.hub.sent_digital)  # pulsed open join

    async def test_close_infers_closed(self):
        await self.cover.async_close_cover()
        self.assertEqual(self.cover.current_cover_position, 0)
        self.assertTrue(self.cover.is_closed)

    async def test_stop_keeps_current(self):
        await self.cover.async_open_cover()
        await self.cover.async_stop_cover()
        # Stop can't infer mid-travel; the last optimistic value is kept.
        self.assertEqual(self.cover.current_cover_position, 100)

    async def test_real_feedback_supersedes_optimistic(self):
        # Once the control system starts reporting position, it wins and the
        # entity upgrades to a slider — no config change.
        from homeassistant.components.cover import CoverEntityFeature
        await self.cover.async_open_cover()
        self.assertEqual(self.cover.current_cover_position, 100)
        self.hub.analog[481] = 42  # control system finally reports position
        await self.cover.process_callback("a481", "42")
        self.assertEqual(self.cover.current_cover_position, 42)
        self.assertTrue(
            self.cover.supported_features & CoverEntityFeature.SET_POSITION
        )

    async def test_restore_optimistic_position(self):
        state = types.SimpleNamespace(attributes={"current_position": 100})
        cover = make_optimistic_cover(self.hub, last_state=state)
        await cover.async_added_to_hass()
        self.assertEqual(cover.current_cover_position, 100)


if __name__ == "__main__":
    unittest.main()
