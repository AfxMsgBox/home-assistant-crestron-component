"""Tests for the crestron.reload service.

Entity definitions live in YAML but are set up through a config entry, and
async_setup (the only reader of YAML) runs once at startup. The service has to
re-read configuration.yaml *and* replace the cached copy in hass.data before
reloading the entry, otherwise the reload just replays the stale config.
"""

import asyncio
import logging
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


# --- HA stand-ins ---------------------------------------------------------
# __init__.py pulls in bridge.py and unique_ids.py, so their imports need
# stubbing too. The reloaded config never reaches them in these tests.


class Template:
    def __init__(self, value=None, hass=None):
        self._value = value

    def async_render(self):
        return self._value


class TrackTemplate:
    def __init__(self, template, variables):
        self.template = template


class Script:
    def __init__(self, hass, sequence, name, domain):
        pass


class ConfigEntryState:
    LOADED = "loaded"
    SETUP_ERROR = "setup_error"


_RELOADED_CONFIG = {}


async def _fake_integration_yaml_config(hass, domain):
    return _RELOADED_CONFIG.get("value")


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.event",
        TrackTemplate=TrackTemplate,
        async_track_template_result=lambda *a, **k: types.SimpleNamespace(
            async_remove=lambda: None
        ),
    )
    _module("homeassistant.helpers.template", Template=Template)
    _module("homeassistant.helpers.script", Script=Script)
    _module(
        "homeassistant.helpers.reload",
        async_integration_yaml_config=_fake_integration_yaml_config,
    )
    _module("homeassistant.helpers.config_validation", port=int, string=str,
            entity_id=str, template=str, ensure_list=list, SCRIPT_SCHEMA=list)
    _module("homeassistant.core", callback=lambda fn: fn, Context=object)
    _module(
        "homeassistant.const",
        EVENT_HOMEASSISTANT_STOP="stop",
        SERVICE_RELOAD="reload",
        CONF_VALUE_TEMPLATE="value_template",
        CONF_ATTRIBUTE="attribute",
        CONF_ENTITY_ID="entity_id",
    )
    _module(
        "homeassistant.config_entries",
        SOURCE_IMPORT="import",
        ConfigEntryState=ConfigEntryState,
    )
    _module(
        "homeassistant.helpers.service",
        async_register_admin_service=lambda *a, **k: None,
    )
    ha.helpers = helpers


_install_stubs()
crestron = load("__init__")
# _invalid_entities imports the nine platform modules, which need far heavier
# stubs than this file's scope; test_platform_schemas covers that validation
# directly. Here the reload flow itself is what matters.
_REAL_INVALID_ENTITIES = crestron._invalid_entities
crestron._invalid_entities = lambda config: ([], [])
logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)

DOMAIN = "crestron"
YAML_CONF = "yaml_config"


class FakeEntry:
    def __init__(self, entry_id):
        self.entry_id = entry_id
        self.state = ConfigEntryState.LOADED


class FakeConfigEntries:
    def __init__(self, entries):
        self._entries = entries
        self.reloaded = []
        # entry_id -> list of states to apply on successive reloads.
        self.outcomes = {}
        # entry_id -> list of boolean return values.
        self.results = {}
        # entry_id -> exception raised while the state stays LOADED.
        self.raise_but_keep_state = {}

    def async_entries(self, domain):
        return list(self._entries)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)
        entry = next(e for e in self._entries if e.entry_id == entry_id)
        boom = self.raise_but_keep_state.pop(entry_id, None)
        if boom is not None:
            raise boom  # state deliberately left LOADED
        pending = self.outcomes.get(entry_id)
        outcome = pending.pop(0) if pending else ConfigEntryState.LOADED
        if isinstance(outcome, Exception):
            entry.state = ConfigEntryState.SETUP_ERROR
            raise outcome
        entry.state = outcome
        results = self.results.get(entry_id)
        return results.pop(0) if results else True


class FakeHass:
    def __init__(self, entries=("entry-1",)):
        self.data = {DOMAIN: {YAML_CONF: {"port": 10200, "light": ["old"]}}}
        self.config_entries = FakeConfigEntries(
            [FakeEntry(e) for e in entries]
        )


class ReloadServiceTests(unittest.TestCase):
    def setUp(self):
        self.hass = FakeHass()

    def _reload(self, new_config):
        _RELOADED_CONFIG["value"] = new_config
        asyncio.run(crestron._async_reload_yaml(self.hass, None))

    def test_reload_replaces_cached_config_and_reloads_entry(self):
        fresh = {"port": 10200, "light": ["new-one", "new-two"]}
        self._reload({DOMAIN: fresh})
        self.assertEqual(self.hass.data[DOMAIN][YAML_CONF], fresh)
        self.assertEqual(self.hass.config_entries.reloaded, ["entry-1"])

    def test_missing_domain_keeps_previous_config(self):
        """A YAML edit that drops `crestron:` must not wipe a working setup."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        self._reload({"other_domain": {}})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        self.assertEqual(self.hass.config_entries.reloaded, [])

    def test_unknown_platform_key_aborts_reload(self):
        """`lights:` is most likely a typo, not an instruction to delete lights."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        logging.getLogger(crestron.__name__).setLevel(logging.WARNING)
        try:
            with self.assertLogs(crestron.__name__, level="WARNING") as logs:
                self._reload(
                    {DOMAIN: {"port": 10200, "lights": [{"name": "客厅灯"}]}}
                )
        finally:
            logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        self.assertEqual(self.hass.config_entries.reloaded, [])
        self.assertTrue(any("Refusing to reload" in m for m in logs.output))

    def test_invalid_yaml_keeps_previous_config(self):
        """async_integration_yaml_config returns None when validation fails."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        self._reload(None)
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        self.assertEqual(self.hass.config_entries.reloaded, [])

    def test_reload_reports_join_conflicts_in_the_new_config(self):
        """The new config gets the same checks async_setup applies."""
        with self.assertLogs(crestron.__name__, level="WARNING") as logs:
            logging.getLogger(crestron.__name__).setLevel(logging.WARNING)
            try:
                self._reload({DOMAIN: {
                    "port": 10200,
                    "light": [
                        {"name": "a", "on_join": 1, "off_join": 2},
                        {"name": "b", "on_join": 1, "off_join": 3},
                    ],
                }})
            finally:
                logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertTrue(any("join conflict" in m for m in logs.output))

    def test_all_entries_are_reloaded(self):
        self.hass = FakeHass(entries=("a", "b"))
        self._reload({DOMAIN: {"port": 1}})
        self.assertEqual(self.hass.config_entries.reloaded, ["a", "b"])

    def test_failed_reload_rolls_back_to_previous_config(self):
        """The port-already-in-use case: don't leave HA with nothing.

        The entry fails to set up under the new config; without a rollback the
        old config is already gone and there are no entities at all.
        """
        before = self.hass.data[DOMAIN][YAML_CONF]
        # First reload attempt fails, the rollback reload succeeds.
        self.hass.config_entries.outcomes["entry-1"] = [
            ConfigEntryState.SETUP_ERROR
        ]
        self._reload({DOMAIN: {"port": 9999, "light": ["new"]}})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        # Reloaded twice: the failed attempt, then the rollback.
        self.assertEqual(
            self.hass.config_entries.reloaded, ["entry-1", "entry-1"]
        )

    def test_failed_reload_logs_an_error(self):
        self.hass.config_entries.outcomes["entry-1"] = [
            ConfigEntryState.SETUP_ERROR
        ]
        logging.getLogger(crestron.__name__).setLevel(logging.ERROR)
        try:
            with self.assertLogs(crestron.__name__, level="ERROR") as logs:
                self._reload({DOMAIN: {"port": 9999}})
        finally:
            logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertTrue(any("rolling back" in m for m in logs.output))

    def test_successful_reload_does_not_roll_back(self):
        fresh = {"port": 10200, "light": ["new"]}
        self._reload({DOMAIN: fresh})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], fresh)
        self.assertEqual(self.hass.config_entries.reloaded, ["entry-1"])

    def test_first_ever_load_failure_has_nothing_to_roll_back_to(self):
        """No previous config: report it rather than restoring None."""
        self.hass.data[DOMAIN].pop(YAML_CONF)
        self.hass.config_entries.outcomes["entry-1"] = [
            ConfigEntryState.SETUP_ERROR
        ]
        self._reload({DOMAIN: {"port": 9999}})
        self.assertEqual(self.hass.config_entries.reloaded, ["entry-1"])

    def test_invalid_entities_are_reported_but_do_not_block(self):
        """Reload must skip bad entries like startup does, not refuse to run."""
        original = crestron._invalid_entities
        crestron._invalid_entities = lambda config: (
            [("light", "坏灯", "no control join")], []
        )
        try:
            logging.getLogger(crestron.__name__).setLevel(logging.WARNING)
            with self.assertLogs(crestron.__name__, level="WARNING") as logs:
                self._reload({DOMAIN: {"port": 10200}})
        finally:
            crestron._invalid_entities = original
            logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertTrue(any("will be skipped" in m for m in logs.output))
        self.assertEqual(self.hass.config_entries.reloaded, ["entry-1"])


    def test_raising_reload_triggers_rollback(self):
        """async_reload can raise; that is still a failed reload."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        self.hass.config_entries.outcomes["entry-1"] = [
            RuntimeError("address already in use")
        ]
        self._reload({DOMAIN: {"port": 9999}})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        self.assertEqual(
            self.hass.config_entries.reloaded, ["entry-1", "entry-1"]
        )

    def test_failed_rollback_is_reported(self):
        """If the old config can't come back either, say so loudly."""
        self.hass.config_entries.outcomes["entry-1"] = [
            ConfigEntryState.SETUP_ERROR,   # new config fails
            ConfigEntryState.SETUP_ERROR,   # and so does the rollback
        ]
        logging.getLogger(crestron.__name__).setLevel(logging.ERROR)
        try:
            with self.assertLogs(crestron.__name__, level="ERROR") as logs:
                self._reload({DOMAIN: {"port": 9999}})
        finally:
            logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertTrue(
            any("Rollback did not restore" in m for m in logs.output)
        )

    def test_structural_error_aborts_the_reload(self):
        """A whole section of the wrong type would delete every entity in it.

        A row-level typo skips one entity; this cannot be interpreted at all,
        so the running configuration must stay.
        """
        original = crestron._invalid_entities
        crestron._invalid_entities = lambda config: (
            [], [("light", "<whole section>", "must be a list of entities")]
        )
        before = self.hass.data[DOMAIN][YAML_CONF]
        try:
            logging.getLogger(crestron.__name__).setLevel(logging.ERROR)
            with self.assertLogs(crestron.__name__, level="ERROR") as logs:
                self._reload({DOMAIN: {"port": 10200, "light": {"name": "x"}}})
        finally:
            crestron._invalid_entities = original
            logging.getLogger(crestron.__name__).setLevel(logging.CRITICAL)
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)
        self.assertEqual(self.hass.config_entries.reloaded, [])
        self.assertTrue(any("Refusing to reload" in m for m in logs.output))

    def test_reload_returning_false_triggers_rollback(self):
        """async_reload has a boolean result; False is a failure too."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        self.hass.config_entries.results["entry-1"] = [False]
        self._reload({DOMAIN: {"port": 9999}})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)

    def test_raising_reload_with_loaded_state_still_rolls_back(self):
        """Exception + state left LOADED must not be reported as success."""
        before = self.hass.data[DOMAIN][YAML_CONF]
        self.hass.config_entries.raise_but_keep_state["entry-1"] = RuntimeError(
            "boom"
        )
        self._reload({DOMAIN: {"port": 9999}})
        self.assertIs(self.hass.data[DOMAIN][YAML_CONF], before)


class SectionShapeTests(unittest.TestCase):
    """A platform key that isn't a list makes every entity under it vanish."""

    def test_non_list_platform_section_is_reported(self):
        original = crestron._platform_schemas
        crestron._platform_schemas = lambda: {"light": lambda cfg: cfg}
        try:
            rows, structural = _REAL_INVALID_ENTITIES(
                {"light": {"name": "写成了映射"}}
            )
        finally:
            crestron._platform_schemas = original
        self.assertEqual(rows, [])
        self.assertEqual(len(structural), 1)
        self.assertEqual(structural[0][0], "light")
        self.assertIn("must be a list", structural[0][2])

    def test_absent_section_is_not_a_problem(self):
        original = crestron._platform_schemas
        crestron._platform_schemas = lambda: {"light": lambda cfg: cfg}
        try:
            self.assertEqual(_REAL_INVALID_ENTITIES({}), ([], []))
        finally:
            crestron._platform_schemas = original


if __name__ == "__main__":
    unittest.main()
