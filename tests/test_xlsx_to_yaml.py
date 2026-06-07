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


class AcTests(unittest.TestCase):
    def _full(self):
        return {"楼层": "B2", "房间": "洗衣房", "开": "505", "关": "506",
                "制冷": "507", "制热": "508", "通风": "510", "除湿": "511",
                "低速": "512", "中速": "513", "高速": "514", "自动": "515",
                "温度": "414", "室温": "415"}

    def test_full_row_produces_five_entities(self):
        result = g.build_ac(self._full())
        by_plat = {}
        for plat, ent in result:
            by_plat.setdefault(plat, []).append(ent)
        # switch + number + select + 2 sensors
        self.assertEqual(len(result), 5)
        self.assertEqual(len(by_plat["sensor"]), 2)

        sw = by_plat["switch"][0]
        self.assertEqual(sw["name"], "B2.洗衣房 空调")
        self.assertEqual((sw["on_join"], sw["off_join"]), (505, 506))
        self.assertEqual(sw["mode_joins"],
                         {"制冷": 507, "制热": 508, "通风": 510, "除湿": 511})

        num = by_plat["number"][0]
        self.assertEqual(num["name"], "B2.洗衣房 空调 温度")
        self.assertEqual(num["value_join"], 414)
        self.assertEqual(num["unit_of_measurement"], "°C")

        sel = by_plat["select"][0]
        self.assertEqual(sel["options"],
                         {"低速": 512, "中速": 513, "高速": 514, "自动": 515})

        names = {e["name"] for e in by_plat["sensor"]}
        self.assertEqual(names, {"B2.洗衣房 空调 室温", "B2.洗衣房 空调 模式"})
        mode_sensor = next(e for e in by_plat["sensor"] if "mode_joins" in e)
        self.assertEqual(mode_sensor["mode_joins"]["除湿"], 511)

        # all five entities belong to one HA device
        device_ids = {e["device_id"] for _, e in result}
        device_names = {e["device_name"] for _, e in result}
        self.assertEqual(device_ids, {"ac_505"})
        self.assertEqual(device_names, {"B2.洗衣房 空调"})

    def test_missing_dry_mode_omitted(self):
        row = self._full()
        row["除湿"] = ""
        sw = next(e for p, e in g.build_ac(row) if p == "switch")
        self.assertNotIn("除湿", sw["mode_joins"])

    def test_empty_row_skipped(self):
        self.assertIsNone(g.build_ac({"楼层": "B2", "房间": "x"}))


class DedupTests(unittest.TestCase):
    def test_duplicate_names_suffixed(self):
        ents = [{"name": "B2.车库 柜灯"}, {"name": "B2.车库 柜灯"},
                {"name": "B2.车库 柜灯"}, {"name": "B2.车库 吊灯"}]
        g.dedup_names(ents)
        self.assertEqual([e["name"] for e in ents],
                         ["B2.车库 柜灯", "B2.车库 柜灯 2",
                          "B2.车库 柜灯 3", "B2.车库 吊灯"])


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


if __name__ == "__main__":
    unittest.main()
