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
    base = {"楼层": "B2", "房间": "车库", "名字": "", "功能": "",
            "亮度": "", "色温": "", "开": "", "关": ""}
    base.update(kw)
    return base


class LightSwitchTests(unittest.TestCase):
    def test_dual_color_temp_light(self):
        plat, ent = g.build_light_or_switch(
            light_row(名字="天花灯带", 功能="双色温", 亮度="1", 色温="201")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["name"], "B2.车库 天花灯带")
        self.assertEqual(ent["type"], "brightness")
        self.assertEqual(ent["brightness_join"], 1)
        self.assertEqual(ent["color_temp_join"], 201)

    def test_brightness_only_light(self):
        plat, ent = g.build_light_or_switch(
            light_row(名字="吊灯", 功能="单色温", 亮度="4")
        )
        self.assertEqual(plat, "light")
        self.assertNotIn("color_temp_join", ent)

    def test_single_label_but_has_color_temp(self):
        # Capability is column-driven: 单色温 row that still carries a 色温
        # join must become a color-temp light.
        plat, ent = g.build_light_or_switch(
            light_row(名字="壁灯", 功能="单色温", 亮度="24", 色温="224")
        )
        self.assertEqual(plat, "light")
        self.assertEqual(ent["color_temp_join"], 224)

    def test_relay_is_switch(self):
        plat, ent = g.build_light_or_switch(
            light_row(名字="柜灯", 功能="Relay", 开="1", 关="2")
        )
        self.assertEqual(plat, "switch")
        self.assertEqual(ent["on_join"], 1)
        self.assertEqual(ent["off_join"], 2)
        self.assertNotIn("type", ent)

    def test_placeholder_skipped(self):
        self.assertIsNone(g.build_light_or_switch(light_row(名字="x", 功能="//")))


class CoverTests(unittest.TestCase):
    def test_curtain(self):
        plat, ent = g.build_cover(
            {"楼层": "B2", "房间": "会客厅", "名称": "纱帘1",
             "开": "700", "关": "701", "停止": "702"}
        )
        self.assertEqual(plat, "cover")
        self.assertEqual(ent["type"], "curtain")
        self.assertEqual(ent["name"], "B2.会客厅 纱帘1")
        self.assertEqual((ent["open_join"], ent["close_join"], ent["stop_join"]),
                         (700, 701, 702))

    def test_roller_is_shade(self):
        _, ent = g.build_cover(
            {"楼层": "1F", "房间": "书房", "名称": "卷1",
             "开": "800", "关": "801", "停止": "802"}
        )
        self.assertEqual(ent["type"], "shade")

    def test_missing_join_skipped(self):
        self.assertIsNone(g.build_cover(
            {"楼层": "B2", "房间": "x", "名称": "纱帘", "开": "1", "关": "2", "停止": ""}
        ))


class ClimateTests(unittest.TestCase):
    def _full(self):
        return {"楼层": "B2", "房间": "洗衣房", "开": "505", "关": "506",
                "制冷": "507", "制热": "508", "通风": "510", "除湿": "511",
                "低速": "512", "中速": "513", "高速": "514", "自动": "515",
                "温度": "414", "室温": "415"}

    def test_full_row(self):
        plat, ent = g.build_climate(self._full())
        self.assertEqual(plat, "climate")
        self.assertEqual(ent["name"], "B2.洗衣房 空调")
        self.assertEqual(ent["on_join"], 505)
        self.assertEqual(ent["mode_dry_join"], 511)
        self.assertEqual(ent["fan_auto_join"], 515)
        self.assertEqual(ent["set_temp_join"], 414)
        self.assertEqual(ent["reg_temp_join"], 415)

    def test_missing_column_omitted(self):
        row = self._full()
        row["除湿"] = ""
        _, ent = g.build_climate(row)
        self.assertNotIn("mode_dry_join", ent)

    def test_empty_row_skipped(self):
        self.assertIsNone(g.build_climate({"楼层": "B2", "房间": "x"}))


class DedupTests(unittest.TestCase):
    def test_duplicate_names_suffixed(self):
        ents = [{"name": "B2.车库 柜灯"}, {"name": "B2.车库 柜灯"},
                {"name": "B2.车库 柜灯"}, {"name": "B2.车库 吊灯"}]
        g.dedup_names(ents)
        self.assertEqual([e["name"] for e in ents],
                         ["B2.车库 柜灯", "B2.车库 柜灯 2",
                          "B2.车库 柜灯 3", "B2.车库 吊灯"])


if __name__ == "__main__":
    unittest.main()
