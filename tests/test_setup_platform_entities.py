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
