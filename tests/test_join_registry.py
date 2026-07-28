"""Tests for runtime join-conflict detection.

The rule under test: two *writers* on one join is a conflict, a reader sharing
a writer's join is the documented feedback/mirroring pattern and must stay
silent, and duplicate to_joins/from_joins keys are a guaranteed silent loss.
"""

import unittest

from loader import load

reg = load("join_registry")


class ConflictTests(unittest.TestCase):
    def test_clean_config_has_no_conflicts(self):
        config = {
            "light": [{"name": "灯", "on_join": 1, "off_join": 2}],
            "cover": [
                {"name": "帘", "open_join": 700, "close_join": 701,
                 "stop_join": 702, "pos_join": 480}
            ],
            "climate": [{"name": "空调", "on_join": 505, "off_join": 506}],
        }
        self.assertEqual(reg.find_conflicts(config), [])

    def test_two_writers_on_one_join_conflict(self):
        config = {
            "light": [{"name": "灯", "on_join": 1, "off_join": 2}],
            "switch": [{"name": "插座", "on_join": 1, "off_join": 3}],
        }
        conflicts = reg.find_conflicts(config)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("d1", conflicts[0])
        self.assertIn("灯", conflicts[0])
        self.assertIn("插座", conflicts[0])

    def test_read_only_mirror_is_not_a_conflict(self):
        """The README's documented pattern: a sensor mirroring AC mode joins."""
        config = {
            "climate": [{
                "name": "空调", "on_join": 505, "off_join": 506,
                "mode_cool_join": 507, "mode_heat_join": 508,
            }],
            "sensor": [{
                "name": "运行模式",
                "mode_joins": {"制冷": 507, "制热": 508},
            }],
            "switch": [{
                "name": "电源", "switch_join": 900,
                "mode_joins": {"制冷": 507},
            }],
            "binary_sensor": [{"name": "运行中", "is_on_join": 507}],
        }
        self.assertEqual(reg.find_conflicts(config), [])

    def test_analog_and_digital_spaces_are_independent(self):
        config = {
            "number": [{"name": "温度", "value_join": 1}],       # a1
            "light": [{"name": "灯", "on_join": 1, "off_join": 2}],  # d1
        }
        self.assertEqual(reg.find_conflicts(config), [])

    def test_to_joins_colliding_with_entity_command_join(self):
        config = {
            "switch": [{"name": "排气扇", "switch_join": 65}],
            "to_joins": [{"join": "d65", "entity_id": "switch.other"}],
        }
        conflicts = reg.find_conflicts(config)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("d65", conflicts[0])
        self.assertIn("to_joins[0]", conflicts[0])

    def test_duplicate_to_joins_keys_reported_as_dropped(self):
        config = {
            "to_joins": [
                {"join": "a35", "entity_id": "sensor.a"},
                {"join": "a35", "entity_id": "sensor.b"},
            ]
        }
        conflicts = reg.find_conflicts(config)
        # Both the "silently dropped" line and the two-writer line apply.
        self.assertTrue(any("only the last one takes effect" in c for c in conflicts))
        self.assertTrue(all("a35" in c for c in conflicts))

    def test_duplicate_from_joins_keys_reported(self):
        config = {
            "from_joins": [
                {"join": "d100", "script": []},
                {"join": "d100", "script": []},
            ]
        }
        conflicts = reg.find_conflicts(config)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("only the last one takes effect", conflicts[0])

    def test_from_joins_reading_an_entity_join_is_silent(self):
        config = {
            "switch": [{"name": "排气扇", "switch_join": 65}],
            "from_joins": [{"join": "d65", "script": []}],
        }
        self.assertEqual(reg.find_conflicts(config), [])

    def test_select_options_conflict_with_climate_fan_joins(self):
        config = {
            "climate": [{
                "name": "空调", "on_join": 505, "off_join": 506,
                "fan_low_join": 512, "fan_high_join": 514,
            }],
            "select": [{"name": "风速", "options": {"低": 512, "高": 514}}],
        }
        conflicts = reg.find_conflicts(config)
        self.assertEqual(len(conflicts), 2)  # d512 and d514

    def test_output_is_ordered_by_join(self):
        config = {
            "light": [
                {"name": "a", "on_join": 9, "off_join": 10},
                {"name": "b", "on_join": 9, "off_join": 11},
                {"name": "c", "on_join": 3, "off_join": 12},
                {"name": "d", "on_join": 3, "off_join": 13},
            ]
        }
        conflicts = reg.find_conflicts(config)
        self.assertEqual(len(conflicts), 2)
        self.assertIn("d3", conflicts[0])
        self.assertIn("d9", conflicts[1])

    def test_malformed_entries_are_ignored(self):
        config = {
            "light": ["not a dict", {"name": "灯", "on_join": "1"}],
            "to_joins": [{"join": "x1"}, {"join": "d"}, {"nope": 1}, "junk"],
        }
        self.assertEqual(reg.find_conflicts(config), [])

    def test_usage_summary_counts_distinct_joins(self):
        config = {"light": [{"name": "灯", "on_join": 1, "off_join": 2}]}
        summary = reg.usage_summary(config)
        self.assertEqual(summary["joins_in_use"], 2)
        self.assertEqual(summary["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
