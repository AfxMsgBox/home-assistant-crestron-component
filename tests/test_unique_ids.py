"""Tests for the final, non-migrating unique-ID rules."""

import logging
import unittest

from loader import load


uid = load("unique_ids")
logging.getLogger(uid.__name__).setLevel(logging.CRITICAL)


class UniqueIdTests(unittest.TestCase):
    def test_dimmable_light_id(self):
        config = {"name": "灯带", "brightness_join": 20}
        self.assertEqual(uid.light_unique_id(config), "crestron_light_20")

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
        low_first = {"name": "风速", "options": {"低": 512, "中": 513, "高": 514}}
        reordered = {"name": "风速", "options": {"高": 514, "低": 512, "中": 513}}
        self.assertEqual(
            uid.select_unique_id(low_first), "crestron_select_512_513_514"
        )
        self.assertEqual(
            uid.select_unique_id(reordered), uid.select_unique_id(low_first)
        )

        modes = {"name": "模式", "mode_joins": {"制热": 508, "制冷": 507}}
        self.assertEqual(
            uid.sensor_unique_id(modes), "crestron_sensor_mode_507_508"
        )

    def test_group_ids_distinguish_overlapping_sets(self):
        a = {"name": "A", "mode_joins": {"制冷": 507, "制热": 508}}
        b = {"name": "B", "mode_joins": {"制冷": 507, "通风": 510}}
        self.assertNotEqual(uid.sensor_unique_id(a), uid.sensor_unique_id(b))

    def test_simple_platform_ids(self):
        self.assertEqual(
            uid.cover_unique_id({"open_join": 700, "close_join": 701}),
            "crestron_cover_d700",
        )
        self.assertEqual(
            uid.climate_unique_id({"on_join": 505, "off_join": 506}),
            "crestron_climate_d505",
        )
        self.assertEqual(
            uid.sensor_unique_id({"value_join": 1}), "crestron_sensor_1"
        )
        self.assertEqual(
            uid.binary_sensor_unique_id({"is_on_join": 2}),
            "crestron_binary_sensor_2",
        )
        self.assertEqual(uid.number_unique_id({"value_join": 3}), "crestron_number_3")
        self.assertEqual(
            uid.media_player_unique_id({"source_number_join": 4}),
            "crestron_media_4",
        )

    def test_cover_ignores_optional_position(self):
        base = {"name": "窗帘", "open_join": 700, "close_join": 701}
        with_pos = {**base, "pos_join": 480}
        self.assertEqual(uid.cover_unique_id(with_pos), uid.cover_unique_id(base))

    def test_climate_ignores_optional_temperature_feedback(self):
        base = {"name": "空调", "on_join": 505, "off_join": 506}
        with_temp = {**base, "set_temp_join": 414, "reg_temp_join": 415}
        self.assertEqual(uid.climate_unique_id(with_temp), uid.climate_unique_id(base))

    def test_duplicate_ids_detected(self):
        config = {
            "sensor": [
                {"name": "模式 A", "mode_joins": {"制冷": 507, "制热": 508}},
                {"name": "模式 B", "mode_joins": {"制热": 508, "制冷": 507}},
            ]
        }
        duplicates = uid.duplicate_unique_ids(config)
        self.assertEqual(len(duplicates), 1)
        self.assertIn("crestron_sensor_mode_507_508", duplicates[0])
        self.assertIn("模式 A", duplicates[0])
        self.assertIn("模式 B", duplicates[0])

    def test_distinct_entities_report_no_duplicates(self):
        config = {
            "light": [
                {"name": "a", "on_join": 1, "off_join": 2},
                {"name": "b", "brightness_join": 5},
            ],
            "switch": [{"name": "c", "switch_join": 1}],
        }
        self.assertEqual(uid.duplicate_unique_ids(config), [])

    def test_duplicate_scan_survives_malformed_entries(self):
        config = {"light": ["junk", {"name": "ok", "on_join": 1, "off_join": 2}]}
        self.assertEqual(uid.duplicate_unique_ids(config), [])


if __name__ == "__main__":
    unittest.main()
