"""执行模板中的真实 Jinja 和动作分支；状态/服务替身不代替 HA 端到端验证。"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment


TEMPLATE = Path(__file__).resolve().parents[1] / "automations_manual_template.yaml"
SWITCH_MAP = {
    "switch.button": {
        "light_entity_ids": ["light.lamp"],
        "switch_entity_ids": ["switch.outlet"],
        "climate_entity_ids": ["climate.ac"],
    },
    "switch.second_button": {"light_entity_ids": ["light.lamp"]},
}
OCCUPANCY_MAP = {
    "binary_sensor.first": {
        "light_entity_ids": ["light.lamp"],
        "switch_entity_ids": ["switch.outlet"],
    },
    "binary_sensor.second": {"light_entity_ids": ["light.lamp"]},
}


def render_fixture(switch_map, occupancy_map):
    """仅供测试的替换示例；生产 Excel 生成器留待后续实现。"""
    feedback = sorted({entity for cfg in switch_map.values()
                       for entities in cfg.values() for entity in entities})
    triggers = []
    for trigger_id, entities, state in [
        ("switch_changed", list(switch_map), None),
        ("crestron_changed", feedback, None),
        ("occupancy_on", list(occupancy_map), "on"),
        ("occupancy_off", list(occupancy_map), "off"),
    ]:
        if entities:
            item = {"trigger": "state", "entity_id": entities,
                    "to": state, "id": trigger_id}
            if trigger_id == "occupancy_off":
                item["for"] = "00:00:30"
            triggers.append(item)
    blocks = {"TRIGGERS": triggers, "SWITCH_MAP": switch_map,
              "OCCUPANCY_MAP": occupancy_map}
    text = TEMPLATE.read_text()
    for name, value in blocks.items():
        pattern = rf"(?m)^( *)@@{name}@@$"
        matches = list(re.finditer(pattern, text))
        assert len(matches) == 1, name
        indent = matches[0][1]
        replacement = "\n".join(indent + line for line in
                                yaml.safe_dump(value, sort_keys=False).splitlines())
        text = re.sub(pattern, lambda _: replacement, text)
    assert "@@" not in text
    return yaml.safe_load(text)


class Scenario:
    """运行本模板所用的动作子集，保留真实模板求值与命令顺序。"""

    def __init__(self, switch_map=None, occupancy_map=None):
        self.config = render_fixture(
            SWITCH_MAP if switch_map is None else switch_map,
            OCCUPANCY_MAP if occupancy_map is None else occupancy_map)
        self.clock = datetime(2026, 10, 11, tzinfo=timezone.utc)
        self.entities = {}
        self.calls = []
        self.waits = []
        self.on_action = None
        self.on_wait = None
        self.env = SandboxedEnvironment(undefined=StrictUndefined)
        self.env.globals.update(
            states=lambda entity: self.entities.get(entity, SimpleNamespace(state="unknown")).state,
            expand=lambda entity: [self.entities[entity]] if entity in self.entities else [],
            now=lambda: self.clock,
            as_timestamp=lambda value: value.timestamp(),
        )

    def set(self, entity, state, age=60):
        self.entities[entity] = SimpleNamespace(
            entity_id=entity, state=state,
            last_changed=self.clock - timedelta(seconds=age))

    def render(self, value, variables):
        if not isinstance(value, str) or not ("{{" in value or "{%" in value):
            return value
        result = self.env.from_string(value).render(variables).strip()
        try:
            return ast.literal_eval(result)
        except (ValueError, SyntaxError):
            return result

    def conditions(self, conditions, variables):
        for condition in conditions:
            if condition["condition"] == "trigger":
                ids = condition["id"]
                if variables["trigger"].id not in (ids if isinstance(ids, list) else [ids]):
                    return False
            elif condition["condition"] == "template":
                value = self.render(condition["value_template"], variables)
                assert isinstance(value, bool), value
                if not value:
                    return False
            else:
                raise AssertionError(condition)
        return True

    def steps(self, steps, variables):
        for step in steps:
            if "variables" in step:
                for key, value in step["variables"].items():
                    variables[key] = self.render(value, variables)
            elif "choose" in step:
                for branch in step["choose"]:
                    if self.conditions(branch["conditions"], variables):
                        self.steps(branch["sequence"], variables.copy())
                        break
            elif "if" in step:
                if self.conditions(step["if"], variables):
                    self.steps(step["then"], variables.copy())
            elif "repeat" in step:
                repeat = step["repeat"]
                for item in self.render(repeat["for_each"], variables):
                    self.steps(repeat["sequence"], dict(variables, repeat={"item": item}))
            elif "action" in step:
                action = self.render(step["action"], variables)
                entities = self.render(step["target"]["entity_id"], variables)
                self.calls.append((action, entities))
                if self.on_action:
                    self.on_action(action, entities)
            elif "wait_template" in step:
                if self.on_wait:
                    self.on_wait()
                self.waits.append((self.render(step["wait_template"], variables),
                                   step["timeout"], step["continue_on_timeout"]))
            else:
                raise AssertionError(step)

    def run(self, trigger_id, entity, before="off", after="on"):
        trigger = SimpleNamespace(id=trigger_id, entity_id=entity,
                                  from_state=SimpleNamespace(state=before),
                                  to_state=SimpleNamespace(state=after))
        self.steps(self.config["actions"], {"trigger": trigger})


class AutomationTemplateTests(unittest.TestCase):
    def test_template_contract_and_all_jinja_compiles(self):
        s = Scenario()
        self.assertEqual(s.config["mode"], "queued")
        self.assertEqual(s.config["max"], 100)
        self.assertEqual(len(s.config["triggers"]), 4)
        self.assertIsNone(s.config["triggers"][0]["to"])
        self.assertEqual(s.config["triggers"][3]["for"], "00:00:30")
        self.assertNotIn("xiaomi_cn_", TEMPLATE.read_text())
        self.assertNotIn("device_id:", TEMPLATE.read_text())

        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str) and ("{{" in value or "{%" in value):
                s.env.from_string(value)
        walk(s.config)

    def test_empty_maps_and_missing_trigger_do_nothing(self):
        s = Scenario({}, {})
        self.assertEqual(s.config["triggers"], [])
        s.run("crestron_changed", "light.missing")
        s.run("occupancy_on", "binary_sensor.missing")
        self.assertEqual(s.calls, [])

    def test_switch_opens_all_three_types_without_mode_or_brightness(self):
        s = Scenario()
        s.set("switch.button", "on")
        for entity in ["light.lamp", "switch.outlet", "climate.ac"]:
            s.set(entity, "off")
        s.run("switch_changed", "switch.button")
        self.assertEqual(s.calls, [("light.turn_on", ["light.lamp"]),
                                  ("switch.turn_on", ["switch.outlet"]),
                                  ("climate.turn_on", ["climate.ac"])])
        self.assertEqual(s.waits, [])

    def test_each_type_feedback_syncs_without_opening_rest(self):
        for active, value in [("light.lamp", "on"), ("switch.outlet", "on"),
                              ("climate.ac", "heat"), ("climate.ac", "fan_only")]:
            with self.subTest(active=active, value=value):
                s = Scenario()
                s.set("switch.button", "off")
                s.set("switch.second_button", "off")
                for entity in ["light.lamp", "switch.outlet", "climate.ac"]:
                    s.set(entity, "off")
                s.set(active, value)
                s.run("crestron_changed", active, after=value)
                expected = [("switch.turn_on", "switch.button")]
                if active == "light.lamp":
                    expected.append(("switch.turn_on", "switch.second_button"))
                self.assertEqual(s.calls, expected)
                s.calls.clear()
                s.set("switch.button", "on")
                s.run("switch_changed", "switch.button")
                self.assertEqual(s.calls, [])

    def test_unknown_missing_or_unavailable_never_counts_as_off(self):
        for state in [None, "unknown", "unavailable", "unexpected"]:
            with self.subTest(state=state):
                s = Scenario()
                s.set("switch.button", "on")
                s.set("light.lamp", "off")
                s.set("switch.outlet", "off")
                if state is not None:
                    s.set("climate.ac", state)
                s.run("crestron_changed", "light.lamp", "on", "off")
                self.assertEqual(s.calls, [])

    def test_any_on_overrides_unknown_other_member(self):
        s = Scenario()
        s.set("switch.button", "off")
        s.set("climate.ac", "heat")
        s.run("crestron_changed", "climate.ac", "unavailable", "heat")
        self.assertEqual(s.calls, [("switch.turn_on", "switch.button")])

    def test_all_off_syncs_switch_and_echo_does_not_resend(self):
        s = Scenario()
        s.set("switch.button", "on")
        for entity in ["light.lamp", "switch.outlet", "climate.ac"]:
            s.set(entity, "off")
        s.run("crestron_changed", "climate.ac", "heat", "off")
        self.assertEqual(s.calls, [("switch.turn_off", "switch.button")])
        s.set("switch.button", "off")
        s.calls.clear()
        s.run("switch_changed", "switch.button", "on", "off")
        self.assertEqual(s.calls, [])

    def test_close_all_commands_precede_one_wait_and_timeout_continues(self):
        s = Scenario()
        s.set("switch.button", "off")
        s.set("light.lamp", "on")
        s.set("switch.outlet", "on")
        s.set("climate.ac", "heat")
        s.on_wait = lambda: self.assertEqual(len(s.calls), 3)
        s.run("switch_changed", "switch.button", "on", "off")
        self.assertEqual(s.calls, [("light.turn_off", ["light.lamp"]),
                                  ("switch.turn_off", ["switch.outlet"]),
                                  ("climate.turn_off", ["climate.ac"])])
        self.assertEqual(s.waits, [(False, "00:00:08", True)])

    def test_wait_rechecks_fresh_feedback(self):
        s = Scenario()
        s.set("switch.button", "off")
        s.set("climate.ac", "heat")
        def feedback():
            for entity in ["light.lamp", "switch.outlet", "climate.ac"]:
                s.set(entity, "off")
        s.on_wait = feedback
        s.run("switch_changed", "switch.button", "on", "off")
        self.assertEqual(s.waits, [(True, "00:00:08", True)])

    def test_stale_queued_switch_event_is_skipped(self):
        s = Scenario()
        s.set("switch.button", "off")
        s.run("switch_changed", "switch.button", "off", "on")
        self.assertEqual(s.calls, [])

    def test_occupancy_opens_light_and_outlet_only(self):
        s = Scenario()
        s.set("binary_sensor.first", "on")
        s.set("binary_sensor.second", "unavailable")
        s.run("occupancy_on", "binary_sensor.first")
        self.assertEqual(s.calls, [("light.turn_on", "light.lamp"),
                                  ("switch.turn_on", "switch.outlet")])

    def test_occupancy_off_is_per_device_and_respects_other_sensor(self):
        s = Scenario()
        s.set("binary_sensor.first", "off", age=30)
        s.set("binary_sensor.second", "on")
        s.run("occupancy_off", "binary_sensor.first", "on", "off")
        self.assertEqual(s.calls, [("switch.turn_off", "switch.outlet")])

    def test_all_sensors_must_be_off_for_full_30_seconds(self):
        for age, expected in [(0, []), (29.99, []), (30, [("light.turn_off", "light.lamp")])]:
            with self.subTest(age=age):
                s = Scenario()
                s.set("binary_sensor.first", "off", age=60)
                s.set("binary_sensor.second", "off", age=age)
                s.run("occupancy_off", "binary_sensor.second", "on", "off")
                self.assertEqual(s.calls, expected)

    def test_unknown_or_missing_sensor_blocks_shutdown(self):
        for state in [None, "unknown", "unavailable"]:
            with self.subTest(state=state):
                s = Scenario()
                s.set("binary_sensor.second", "off", age=60)
                if state is not None:
                    s.set("binary_sensor.first", state)
                s.run("occupancy_off", "binary_sensor.second", "on", "off")
                self.assertEqual(s.calls, [])

    def test_stale_on_event_and_returning_occupancy_do_not_misoperate(self):
        s = Scenario()
        s.set("binary_sensor.first", "off", age=1)
        s.set("binary_sensor.second", "off", age=60)
        s.run("occupancy_on", "binary_sensor.first")
        self.assertEqual(s.calls, [])
        s.set("binary_sensor.first", "on", age=0)
        s.run("occupancy_off", "binary_sensor.first", "on", "off")
        self.assertEqual(s.calls, [])

    def test_recheck_occupancy_between_target_commands(self):
        s = Scenario()
        s.set("binary_sensor.first", "off", age=60)
        s.set("binary_sensor.second", "off", age=60)
        s.on_action = lambda *_: s.set("binary_sensor.first", "on", age=0)
        s.run("occupancy_off", "binary_sensor.first", "on", "off")
        self.assertEqual(s.calls, [("light.turn_off", "light.lamp")])


if __name__ == "__main__":
    unittest.main()
