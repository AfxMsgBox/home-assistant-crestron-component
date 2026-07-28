"""Tests for the xlsx -> YAML row mapping (tools/xlsx_to_yaml.py).

Only the pure row->entity builders and name de-duplication are tested; they
need no real workbook.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import xlsx_to_yaml as g  # noqa: E402


def light_row(**kw):
    base = {"楼层": "B2", "房间": "车库", "名称": "", "功能": "",
            "亮度": "", "色温": "", "开": "", "关": ""}
    base.update(kw)
    return base


def outlet_row(**kw):
    base = {"楼层": "B2", "房间": "会客厅", "名称": "",
            "开": "", "关": ""}
    base.update(kw)
    return base


class LightTests(unittest.TestCase):
    def test_dual_color_temp_light(self):
        plat, ent = g.build_light(
            light_row(名称="天花灯带", 功能="双色温", 亮度="1", 色温="201")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["name"], "B2.车库 天花灯带")
        self.assertEqual(ent["type"], "color_temp")
        self.assertEqual(ent["brightness_join"], 1)
        self.assertEqual(ent["color_temp_join"], 201)
        self.assertEqual(ent["suggested_area"], "B2.车库")

    def test_brightness_only_light(self):
        plat, ent = g.build_light(
            light_row(名称="吊灯", 功能="单色温", 亮度="4")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["type"], "brightness")
        self.assertNotIn("color_temp_join", ent)

    def test_single_label_but_has_color_temp(self):
        # Capability is column-driven: 单色温 row that still carries a 色温
        # join must become a color-temp light.
        plat, ent = g.build_light(
            light_row(名称="壁灯", 功能="单色温", 亮度="24", 色温="224")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["type"], "color_temp")
        self.assertEqual(ent["color_temp_join"], 224)

    def test_relay_is_onoff_light(self):
        # An on/off-only light stays a light (no brightness), never a switch.
        plat, ent = g.build_light(
            light_row(名称="柜灯", 功能="Relay", 开="1", 关="2")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["type"], "onoff")
        self.assertEqual(ent["on_join"], 1)
        self.assertEqual(ent["off_join"], 2)
        self.assertNotIn("brightness_join", ent)
        self.assertEqual(ent["device_id"], "light_onoff_1")

    def test_color_temp_without_brightness_is_skipped(self):
        self.assertIsNone(
            g.build_light(light_row(名称="错误灯", 色温="201"))
        )

    def test_incomplete_onoff_pair_is_skipped(self):
        self.assertIsNone(
            g.build_light(light_row(名称="错误灯", 开="1"))
        )

    def test_mixed_analog_and_digital_control_is_skipped(self):
        self.assertIsNone(
            g.build_light(
                light_row(名称="歧义灯", 亮度="1", 开="1", 关="2")
            )
        )

    def test_placeholder_skipped(self):
        self.assertIsNone(g.build_light(light_row(名称="x", 功能="//")))


class OutletTests(unittest.TestCase):
    def test_outlet_is_switch_with_outlet_class(self):
        plat, ent = g.build_outlet(
            outlet_row(名称="落地灯", 开="147", 关="148")
        )
        self.assertEqual(plat, "switch")
        self.assertEqual(ent["name"], "B2.会客厅 落地灯")
        self.assertEqual(ent["on_join"], 147)
        self.assertEqual(ent["off_join"], 148)
        self.assertEqual(ent["device_class"], "outlet")
        self.assertEqual(ent["device_id"], "outlet_147")
        self.assertEqual(ent["device_name"], "B2.会客厅 落地灯")
        self.assertEqual(ent["suggested_area"], "B2.会客厅")

    def test_missing_outlet_join_skipped(self):
        self.assertIsNone(g.build_outlet(outlet_row(名称="落地灯", 开="147")))

    def test_outlet_sheet_registered(self):
        self.assertIs(g.SHEET_BUILDERS["插座"], g.build_outlet)


class CoverTests(unittest.TestCase):
    def test_curtain_with_position(self):
        plat, ent = g.build_cover(
            {"楼层": "B2", "房间": "会客厅", "名称": "纱帘1",
             "类型": "curtain", "开": "700", "关": "701",
             "停止": "702", "位置": "480"}
        )
        self.assertEqual(plat, "cover")
        self.assertEqual(ent["type"], "curtain")
        self.assertEqual(ent["name"], "B2.会客厅 纱帘1")
        self.assertEqual((ent["open_join"], ent["close_join"], ent["stop_join"]),
                         (700, 701, 702))
        self.assertEqual(ent["pos_join"], 480)

    def test_position_optional(self):
        # No 位置 column (or blank) -> no pos_join, still a valid cover.
        _, ent = g.build_cover(
            {"楼层": "B2", "房间": "会客厅", "名称": "纱帘1",
             "开": "700", "关": "701", "停止": "702"}
        )
        self.assertNotIn("pos_join", ent)
        _, ent2 = g.build_cover(
            {"楼层": "B2", "房间": "会客厅", "名称": "纱帘1",
             "开": "700", "关": "701", "停止": "702", "位置": ""}
        )
        self.assertNotIn("pos_join", ent2)

    def test_all_supported_cover_types(self):
        for cover_type in sorted(g.SUPPORTED_COVER_TYPES):
            with self.subTest(cover_type=cover_type):
                _, ent = g.build_cover(
                    {"楼层": "1F", "房间": "书房", "名称": "窗帘",
                     "类型": cover_type, "开": "800", "关": "801",
                     "停止": "802", "位置": "490"}
                )
                self.assertEqual(ent["type"], cover_type)

    def test_cover_type_is_trimmed_and_case_insensitive(self):
        _, ent = g.build_cover(
            {"楼层": "1F", "房间": "书房", "名称": "窗帘",
             "类型": " Shade ", "开": "800", "关": "801", "停止": "802"}
        )
        self.assertEqual(ent["type"], "shade")

    def test_blank_or_unknown_cover_type_defaults_to_curtain(self):
        for cover_type in ("", None, "unsupported"):
            with self.subTest(cover_type=cover_type):
                _, ent = g.build_cover(
                    {"楼层": "1F", "房间": "书房", "名称": "卷1",
                     "类型": cover_type, "开": "800", "关": "801",
                     "停止": "802"}
                )
                # The name no longer guesses the type: even 卷1 defaults to
                # curtain when 类型 is blank or unsupported.
                self.assertEqual(ent["type"], "curtain")

    def test_missing_join_skipped(self):
        # Position present but a required drive join missing -> still skipped.
        self.assertIsNone(g.build_cover(
            {"楼层": "B2", "房间": "x", "名称": "纱帘",
             "开": "1", "关": "2", "停止": "", "位置": "480"}
        ))


class AcTests(unittest.TestCase):
    def _full(self):
        # Column is named 自动 (not 自动风速) in the current 空调 sheet.
        return {"楼层": "B2", "房间": "洗衣房", "开": "505", "关": "506",
                "制冷": "507", "制热": "508", "通风": "510", "除湿": "511",
                "低速": "512", "中速": "513", "高速": "514", "自动": "515",
                "温度": "414", "室温": "415"}

    def test_full_row_produces_one_climate(self):
        plat, ent = g.build_ac(self._full())
        self.assertEqual(plat, "climate")
        self.assertEqual(ent["name"], "B2.洗衣房 空调")
        self.assertEqual((ent["on_join"], ent["off_join"]), (505, 506))
        self.assertEqual(ent["set_temp_join"], 414)
        self.assertEqual(ent["reg_temp_join"], 415)
        self.assertEqual(
            (ent["mode_cool_join"], ent["mode_heat_join"],
             ent["mode_fan_join"], ent["mode_dry_join"]),
            (507, 508, 510, 511),
        )
        self.assertEqual(
            (ent["fan_low_join"], ent["fan_med_join"],
             ent["fan_high_join"], ent["fan_auto_join"]),
            (512, 513, 514, 515),
        )
        self.assertEqual(ent["device_id"], "ac_505")
        self.assertEqual(ent["device_name"], "B2.洗衣房 空调")

    def test_auto_fan_column_mapped(self):
        # Regression: the column is 自动 (was mistakenly read as 自动风速),
        # so its join must land in fan_auto_join.
        _, ent = g.build_ac(self._full())
        self.assertEqual(ent["fan_auto_join"], 515)

    def test_fan_value_column_ignored(self):
        # 风速值 (analog fan speed) is intentionally not consumed — digital
        # fan joins already express fan speed; no fan_value_join is emitted.
        row = self._full()
        row["风速值"] = "457"
        _, ent = g.build_ac(row)
        self.assertNotIn("fan_value_join", ent)
        self.assertNotIn("风速值", ent.values())

    def test_missing_dry_mode_omitted(self):
        row = self._full()
        row["除湿"] = ""
        _, ent = g.build_ac(row)
        self.assertNotIn("mode_dry_join", ent)

    def test_no_power_skipped(self):
        row = self._full()
        row["开"] = ""
        row["关"] = ""
        self.assertIsNone(g.build_ac(row))

    def test_empty_row_skipped(self):
        self.assertIsNone(g.build_ac({"楼层": "B2", "房间": "x"}))


class DedupTests(unittest.TestCase):
    def test_duplicate_names_suffixed(self):
        ents = [
            {"name": "B2.车库 柜灯", "device_name": "B2.车库 柜灯"},
            {"name": "B2.车库 柜灯", "device_name": "B2.车库 柜灯"},
            {"name": "B2.车库 柜灯", "device_name": "B2.车库 柜灯"},
            {"name": "B2.车库 吊灯", "device_name": "B2.车库 吊灯"},
        ]
        g.dedup_names(ents)
        self.assertEqual([e["name"] for e in ents],
                         ["B2.车库 柜灯", "B2.车库 柜灯 2",
                          "B2.车库 柜灯 3", "B2.车库 吊灯"])
        self.assertEqual(
            [e["device_name"] for e in ents],
            [
                "B2.车库 柜灯",
                "B2.车库 柜灯 2",
                "B2.车库 柜灯 3",
                "B2.车库 吊灯",
            ],
        )


class ValidationTests(unittest.TestCase):
    def test_invalid_join_format_and_range_reported(self):
        row = light_row(名称="错误灯", 亮度="a20")
        self.assertIn("亮度: 必须只填写十进制数字", g._row_errors("灯光", row))
        row["亮度"] = "1025"
        self.assertIn("亮度: 必须在 1–1024 范围内", g._row_errors("灯光", row))

    def test_mixed_light_controls_have_specific_error(self):
        errors = g._row_errors(
            "灯光",
            light_row(名称="歧义灯", 亮度="20", 开="10", 关="11"),
        )
        self.assertIn(
            "模拟量控制（亮度/色温）不能与数字量控制（开/关）混填",
            errors,
        )

    def test_required_cover_joins_reported(self):
        errors = g._row_errors(
            "窗帘",
            {
                "楼层": "2F",
                "房间": "主卧",
                "名称": "窗帘",
                "开": "700",
                "关": "",
                "停止": "",
            },
        )
        self.assertEqual(errors, ["缺少必填 Join: 关, 停止"])


class EmitDomainTests(unittest.TestCase):
    def test_domain_yaml_structure(self):
        by_platform = {
            "switch": [{
                "platform": "crestron", "name": "B2.洗衣房 空调",
                "on_join": 505, "mode_joins": {"制冷": 507},
                "device_id": "ac_505", "device_name": "B2.洗衣房 空调",
                "_group": "B2.洗衣房",
            }],
            "number": [{
                "platform": "crestron", "name": "B2.洗衣房 空调 温度",
                "value_join": 414, "device_id": "ac_505", "_group": "B2.洗衣房",
            }],
        }
        out = g.emit_domain(by_platform, "x.xlsx")
        # top-level mapping with port + platform sections; no stray list root
        self.assertIn("\nport: 10200", out)
        self.assertIn("\nswitch:\n", out)
        self.assertIn("\nnumber:\n", out)
        # legacy 'platform:' key and internal '_group' must not be emitted
        self.assertNotIn("platform: crestron", out)
        self.assertNotIn("_group", out)
        # entities are indented one level under their platform key
        self.assertIn('\n  - name: "B2.洗衣房 空调"', out)
        # nested dict (mode_joins) renders as a block
        self.assertIn("\n    mode_joins:\n", out)
        self.assertIn('\n      "制冷": 507', out)


class ScalarEscapingTests(unittest.TestCase):
    """A raw newline in a double-quoted scalar folds or breaks the document.

    Excel cells can contain alt-enter line breaks and stray control characters,
    which used to be written straight through into the generated YAML.
    """

    def test_newline_and_tab_escaped(self):
        self.assertEqual(g._yaml_scalar("a\nb"), '"a\\nb"')
        self.assertEqual(g._yaml_scalar("a\tb"), '"a\\tb"')
        self.assertEqual(g._yaml_scalar("a\rb"), '"a\\rb"')

    def test_other_control_chars_escaped(self):
        self.assertEqual(g._yaml_scalar("a\x00b"), '"a\\x00b"')
        self.assertEqual(g._yaml_scalar("a\x7fb"), '"a\\x7fb"')

    def test_quotes_and_backslashes_still_escaped(self):
        self.assertEqual(g._yaml_scalar('a"b'), '"a\\"b"')
        self.assertEqual(g._yaml_scalar("a\\b"), '"a\\\\b"')

    def test_ordinary_values_unchanged(self):
        self.assertEqual(g._yaml_scalar("curtain"), "curtain")
        self.assertEqual(g._yaml_scalar("1F.客厅 灯带"), '"1F.客厅 灯带"')
        self.assertEqual(g._yaml_scalar(42), "42")

    def test_escaped_output_round_trips(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        raw = "客厅\n灯带\t2"
        parsed = yaml.safe_load("name: " + g._yaml_scalar(raw))
        self.assertEqual(parsed["name"], raw)


class OutputPathTests(unittest.TestCase):
    def test_output_directory_appends_default_filename(self):
        self.assertEqual(
            g._resolve_output_path("../custom_components/crestron"),
            ("../custom_components/crestron",
             "../custom_components/crestron/crestron.yaml"),
        )

    def test_explicit_yaml_path_is_used_verbatim(self):
        self.assertEqual(
            g._resolve_output_path(
                "../custom_components/crestron/crestron.yaml"
            ),
            ("../custom_components/crestron",
             "../custom_components/crestron/crestron.yaml"),
        )

    def test_filename_in_current_directory(self):
        self.assertEqual(
            g._resolve_output_path("crestron.yml"),
            (".", "crestron.yml"),
        )


if __name__ == "__main__":
    unittest.main()
