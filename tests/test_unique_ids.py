"""Tests for stable control-join IDs and registry migration planning."""

import asyncio
from dataclasses import dataclass
import logging
import sys
import types
import unittest

from loader import load


uid = load("unique_ids")
logging.getLogger(uid.__name__).setLevel(logging.CRITICAL)


class UniqueIdPlanningTests(unittest.TestCase):
    def test_dimmable_light_id_is_already_stable(self):
        config = {"name": "灯带", "brightness_join": 20}
        self.assertEqual(uid.light_unique_id(config), "crestron_light_20")
        self.assertEqual(uid.unique_id_migrations({"light": [config]}), [])

    def test_relay_light_ignores_name_and_optional_feedback(self):
        base = {"name": "壁灯", "on_join": 10, "off_join": 11}
        feedback = {**base, "name": "壁灯新名称", "state_join": 12}
        self.assertEqual(uid.light_unique_id(base), "crestron_light_onoff_d10")
        self.assertEqual(uid.light_unique_id(feedback), uid.light_unique_id(base))

    def test_switch_ignores_name_and_optional_feedback(self):
        base = {"name": "插座", "on_join": 147, "off_join": 148}
        feedback = {**base, "name": "新名称", "state_join": 149}
        self.assertEqual(uid.switch_unique_id(base), "crestron_switch_d147")
        self.assertEqual(uid.switch_unique_id(feedback), uid.switch_unique_id(base))

    def test_group_ids_ignore_yaml_option_order(self):
        """Reordering options in YAML must not orphan the entity.

        These IDs used to take whichever join was listed first, so an editing
        convenience silently created a second entity and dropped the history.
        """
        low_first = {"name": "风速", "options": {"低": 512, "中": 513, "高": 514}}
        reordered = {"name": "风速", "options": {"高": 514, "低": 512, "中": 513}}
        self.assertEqual(uid.select_unique_id(low_first), "crestron_select_512")
        self.assertEqual(uid.select_unique_id(reordered), "crestron_select_512")

        modes = {"name": "模式", "mode_joins": {"制热": 508, "制冷": 507}}
        self.assertEqual(uid.sensor_unique_id(modes), "crestron_sensor_mode_507")

    def test_analog_sensor_id_unchanged(self):
        config = {"name": "室外温度", "value_join": 1}
        self.assertEqual(uid.sensor_unique_id(config), "crestron_sensor_1")

    def test_reordered_group_plans_a_migration(self):
        """An entity registered under the old first-listed join is preserved."""
        config = {"name": "风速", "options": {"高": 514, "低": 512}}
        migrations = uid.unique_id_migrations({"select": [config]})
        self.assertEqual(
            [(m.entity_domain, m.old_unique_id, m.new_unique_id) for m in migrations],
            [("select", "crestron_select_514", "crestron_select_512")],
        )

    def test_group_already_lowest_first_needs_no_migration(self):
        config = {"name": "风速", "options": {"低": 512, "高": 514}}
        self.assertEqual(uid.unique_id_migrations({"select": [config]}), [])

    def test_duplicate_ids_detected(self):
        """Two mode sensors sharing their lowest join collide silently in HA.

        The join-conflict check deliberately stays quiet about read/read
        sharing, so this is the only thing that catches it.
        """
        config = {
            "sensor": [
                {"name": "模式 A", "mode_joins": {"制冷": 507, "制热": 508}},
                {"name": "模式 B", "mode_joins": {"制冷": 507, "通风": 510}},
            ]
        }
        dupes = uid.duplicate_unique_ids(config)
        self.assertEqual(len(dupes), 1)
        self.assertIn("crestron_sensor_mode_507", dupes[0])
        self.assertIn("模式 A", dupes[0])
        self.assertIn("模式 B", dupes[0])

    def test_distinct_entities_report_no_duplicates(self):
        config = {
            "light": [
                {"name": "a", "on_join": 1, "off_join": 2},
                {"name": "b", "brightness_join": 5},
            ],
            "switch": [{"name": "c", "switch_join": 1}],  # other platform
        }
        self.assertEqual(uid.duplicate_unique_ids(config), [])

    def test_duplicate_scan_survives_malformed_entries(self):
        config = {"light": ["junk", {"name": "ok", "on_join": 1, "off_join": 2}]}
        self.assertEqual(uid.duplicate_unique_ids(config), [])

    def test_cover_ignores_optional_position(self):
        base = {"name": "窗帘", "open_join": 700, "close_join": 701}
        with_pos = {**base, "pos_join": 480}
        self.assertEqual(uid.cover_unique_id(base), "crestron_cover_d700")
        self.assertEqual(uid.cover_unique_id(with_pos), uid.cover_unique_id(base))

    def test_climate_ignores_optional_temperature_feedback(self):
        base = {"name": "空调", "on_join": 505, "off_join": 506}
        with_temp = {**base, "set_temp_join": 414, "reg_temp_join": 415}
        self.assertEqual(uid.climate_unique_id(base), "crestron_climate_d505")
        self.assertEqual(uid.climate_unique_id(with_temp), uid.climate_unique_id(base))

    def test_plans_current_historical_formats(self):
        config = {
            "light": [
                {"name": "壁灯", "on_join": 10, "off_join": 11},
            ],
            "switch": [
                {"name": "插座", "on_join": 147, "off_join": 148},
            ],
            "cover": [
                {
                    "name": "窗帘",
                    "open_join": 700,
                    "close_join": 701,
                    "pos_join": 480,
                },
            ],
            "climate": [
                {
                    "name": "空调",
                    "on_join": 505,
                    "off_join": 506,
                    "set_temp_join": 414,
                    "reg_temp_join": 415,
                },
            ],
        }
        planned = {
            (m.entity_domain, m.old_unique_id, m.new_unique_id)
            for m in uid.unique_id_migrations(config)
        }
        self.assertEqual(
            planned,
            {
                (
                    "light",
                    "crestron_light_onoff_10_壁灯",
                    "crestron_light_onoff_d10",
                ),
                (
                    "switch",
                    "crestron_switch_147_插座",
                    "crestron_switch_d147",
                ),
                ("cover", "crestron_cover_480", "crestron_cover_d700"),
                ("climate", "crestron_climate_415", "crestron_climate_d505"),
            },
        )


@dataclass
class FakeEntry:
    entity_id: str
    platform: str
    unique_id: str
    device_id: str | None = None


class FakeRegistry:
    def __init__(self, entries):
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get_entity_id(self, domain, platform, unique_id):
        for entry in self.entities.values():
            if (
                entry.entity_id.startswith(f"{domain}.")
                and entry.platform == platform
                and entry.unique_id == unique_id
            ):
                return entry.entity_id
        return None

    def async_update_entity(self, entity_id, *, new_unique_id):
        self.entities[entity_id].unique_id = new_unique_id


class FakeDeviceRegistry:
    def __init__(self, devices=None):
        self.devices = devices or {}

    def async_get_device(self, *, identifiers):
        identifier = next(iter(identifiers))
        device_id = self.devices.get(identifier)
        if device_id is None:
            return None
        return types.SimpleNamespace(id=device_id)


class RegistryMigrationTests(unittest.TestCase):
    def _run(self, registry, config, device_registry=None):
        helpers = sys.modules.get("homeassistant.helpers")
        if helpers is None:
            helpers = types.ModuleType("homeassistant.helpers")
            sys.modules["homeassistant.helpers"] = helpers
        entity_module = types.ModuleType("homeassistant.helpers.entity_registry")
        entity_module.async_get = lambda hass: registry
        helpers.entity_registry = entity_module
        sys.modules["homeassistant.helpers.entity_registry"] = entity_module
        device_module = types.ModuleType("homeassistant.helpers.device_registry")
        device_module.async_get = lambda hass: (
            device_registry or FakeDeviceRegistry()
        )
        helpers.device_registry = device_module
        sys.modules["homeassistant.helpers.device_registry"] = device_module
        asyncio.run(uid.async_migrate_unique_ids(object(), config))

    def test_exact_current_name_is_migrated(self):
        registry = FakeRegistry(
            [
                FakeEntry(
                    "switch.outlet",
                    "crestron",
                    "crestron_switch_147_插座",
                )
            ]
        )
        self._run(
            registry,
            {
                "switch": [
                    {"name": "插座", "on_join": 147, "off_join": 148}
                ]
            },
        )
        self.assertEqual(
            registry.entities["switch.outlet"].unique_id,
            "crestron_switch_d147",
        )

    def test_single_renamed_legacy_entity_is_preserved(self):
        registry = FakeRegistry(
            [
                FakeEntry(
                    "light.old_name",
                    "crestron",
                    "crestron_light_onoff_10_旧名称",
                )
            ]
        )
        self._run(
            registry,
            {
                "light": [
                    {"name": "新名称", "on_join": 10, "off_join": 11}
                ]
            },
        )
        self.assertEqual(
            registry.entities["light.old_name"].unique_id,
            "crestron_light_onoff_d10",
        )

    def test_ambiguous_stale_duplicates_are_not_guessed(self):
        registry = FakeRegistry(
            [
                FakeEntry(
                    "switch.old_1",
                    "crestron",
                    "crestron_switch_147_旧名称",
                ),
                FakeEntry(
                    "switch.old_2",
                    "crestron",
                    "crestron_switch_147_另一个名称",
                ),
            ]
        )
        self._run(
            registry,
            {
                "switch": [
                    {"name": "当前名称", "on_join": 147, "off_join": 148}
                ]
            },
        )
        self.assertEqual(
            {entry.unique_id for entry in registry.entities.values()},
            {
                "crestron_switch_147_旧名称",
                "crestron_switch_147_另一个名称",
            },
        )

    def test_device_identifier_recovers_removed_optional_feedback_id(self):
        registry = FakeRegistry(
            [
                FakeEntry(
                    "cover.bedroom",
                    "crestron",
                    "crestron_cover_480",
                    device_id="device-cover-700",
                )
            ]
        )
        devices = FakeDeviceRegistry(
            {("crestron", "cover_700"): "device-cover-700"}
        )
        self._run(
            registry,
            {
                "cover": [
                    {
                        "name": "窗帘",
                        "open_join": 700,
                        "close_join": 701,
                        "device_id": "cover_700",
                    }
                ]
            },
            devices,
        )
        self.assertEqual(
            registry.entities["cover.bedroom"].unique_id,
            "crestron_cover_d700",
        )


if __name__ == "__main__":
    unittest.main()
