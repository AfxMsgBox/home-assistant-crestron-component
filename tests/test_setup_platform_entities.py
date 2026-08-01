"""Tests for setup_platform_entities: per-entity config-error isolation.

entity.py only imports the pure .const module, so it loads in the bare test
environment via the synthetic-package loader (no Home Assistant needed).
"""

import logging
import unittest

from loader import load

# const.py is pure; load it under the synthetic package so entity.py's
# `from .const import ...` resolves.
load("const")
entity = load("entity")

# The isolation tests intentionally trigger the helper's own
# _LOGGER.exception(...) on skipped entities; silence it (including the
# deliberate traceback) so expected noise doesn't clutter test output.
logging.getLogger(entity.__name__).setLevel(logging.CRITICAL)
from crestron_under_test.const import DOMAIN, HUB, YAML_CONF  # noqa: E402


class FakeHass:
    """Minimal hass.data shape: {DOMAIN: {HUB: hub, YAML_CONF: {...}}}."""

    def __init__(self, hub, yaml_conf):
        self.data = {DOMAIN: {HUB: hub, YAML_CONF: yaml_conf}}


SENTINEL_HUB = object()


def good_factory(hub, cfg):
    # cfg is the "validated" dict; echo it back wrapped so the test can inspect.
    return ("entity", hub, cfg["name"])


def strict_schema(item):
    # Stand-in validator: reject entries lacking a name (like a vol.Required).
    if "name" not in item:
        raise ValueError("name required")
    return item


class JoinUidTests(unittest.TestCase):
    """join_uid keeps the analog and digital join spaces from colliding.

    a1..a1024 and d1..d4096 are unrelated signals, so "first configured join
    wins" can give two entities the same unique_id and make HA silently drop
    one of them.
    """

    def test_analog_wins_and_stays_bare(self):
        # Analog joins stay bare; digital joins are explicitly namespaced.
        self.assertEqual(entity.join_uid(analog=(480,), digital=(704, 705)), "480")

    def test_digital_fallback_is_prefixed(self):
        self.assertEqual(entity.join_uid(analog=(None,), digital=(704, 705)), "d704")

    def test_same_number_in_each_space_does_not_collide(self):
        analog_side = entity.join_uid(analog=(480,), digital=(None, None))
        digital_side = entity.join_uid(analog=(None,), digital=(480, None))
        self.assertNotEqual(analog_side, digital_side)

    def test_preference_order_within_a_space(self):
        self.assertEqual(entity.join_uid(analog=(415, 414)), "415")
        self.assertEqual(entity.join_uid(analog=(None, 414)), "414")
        self.assertEqual(entity.join_uid(digital=(None, 706)), "d706")

    def test_nothing_configured(self):
        self.assertIsNone(entity.join_uid())
        self.assertIsNone(entity.join_uid(analog=(None,), digital=(None,)))


class SetupPlatformEntitiesTests(unittest.TestCase):
    def _run(self, items, schema=strict_schema, factory=good_factory):
        hass = FakeHass(SENTINEL_HUB, {"light": items})
        return entity.setup_platform_entities(hass, "light", schema, factory)

    def test_all_valid(self):
        out = self._run([{"name": "a"}, {"name": "b"}])
        self.assertEqual(
            out,
            [("entity", SENTINEL_HUB, "a"), ("entity", SENTINEL_HUB, "b")],
        )

    def test_bad_entity_skipped_others_survive(self):
        # Middle entry fails schema validation; the other two still load.
        out = self._run([{"name": "a"}, {"oops": 1}, {"name": "c"}])
        self.assertEqual(
            out,
            [("entity", SENTINEL_HUB, "a"), ("entity", SENTINEL_HUB, "c")],
        )

    def test_factory_error_isolated(self):
        # A factory that raises for one entity must not abort the platform.
        def picky_factory(hub, cfg):
            if cfg["name"] == "bad":
                raise RuntimeError("construction failed")
            return ("entity", hub, cfg["name"])

        out = self._run(
            [{"name": "ok"}, {"name": "bad"}, {"name": "ok2"}],
            factory=picky_factory,
        )
        self.assertEqual(
            out,
            [("entity", SENTINEL_HUB, "ok"), ("entity", SENTINEL_HUB, "ok2")],
        )

    def test_missing_platform_key_returns_empty(self):
        hass = FakeHass(SENTINEL_HUB, {})  # no "light" key
        out = entity.setup_platform_entities(
            hass, "light", strict_schema, good_factory
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
