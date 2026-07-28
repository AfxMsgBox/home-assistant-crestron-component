"""Behaviour tests for ToJoinBridge / FromJoinBridge.

bridge.py imports homeassistant, which isn't installed in the bare test
environment, so minimal stand-ins are installed into sys.modules before
loading the module via the synthetic-package loader (same approach as
test_onoff_light / test_climate_filter).

These cover the wiring that moved out of CrestronHub:
  - ToJoinBridge.sync_all re-renders every template and pushes coerced values,
    isolating per-join render failures.
  - ToJoinBridge._set_join routes by join kind and drops unknown values.
  - FromJoinBridge runs the configured script, with digital rising-edge gating.
"""

import asyncio
import contextlib
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


class Template:
    """Minimal Template: holds a value (or an exception to raise on render)."""

    def __init__(self, value=None):
        self._value = value

    def async_render(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _track_template_result(hass, track_templates, action):
    # Return a tracker stub; tests drive _template_change directly.
    return types.SimpleNamespace(async_remove=lambda: None)


class TrackTemplate:
    def __init__(self, template, variables):
        self.template = template


def _callback(fn):
    return fn


class Context:
    pass


class Script:
    """Records runs; mimics async_run(variables, context)."""

    last_instances = []

    def __init__(self, hass, sequence, name, domain):
        self.sequence = sequence
        self.runs = []
        Script.last_instances.append(self)

    async def async_run(self, variables, context):
        self.runs.append(variables)


def _install_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.event",
        TrackTemplate=TrackTemplate,
        async_track_template_result=_track_template_result,
    )
    _module("homeassistant.helpers.template", Template=Template)
    _module("homeassistant.helpers.script", Script=Script)
    _module("homeassistant.core", callback=_callback, Context=Context)
    _module(
        "homeassistant.const",
        CONF_VALUE_TEMPLATE="value_template",
        CONF_ATTRIBUTE="attribute",
        CONF_ENTITY_ID="entity_id",
    )
    ha.helpers = helpers


_install_stubs()
bridge = load("bridge")
CONF_JOIN = "join"
CONF_SCRIPT = "script"

# sync_all's per-join failure isolation deliberately logs an exception with a
# traceback; silence it so expected noise doesn't clutter test output.
logging.getLogger(bridge.__name__).setLevel(logging.CRITICAL)


class FakeHub:
    def __init__(self):
        self.digital = {}
        self.analog = {}
        self.serial = {}
        self.callbacks = []  # (cb, joins)

    def set_digital(self, n, v):
        self.digital[n] = v

    def set_analog(self, n, v):
        self.analog[n] = v

    def set_serial(self, n, v):
        self.serial[n] = v

    def batched_writes(self):
        # The real hub coalesces every set_* in the block into one socket
        # write; for the fake the frames land in the same dicts either way,
        # so a null context manager is enough.
        return contextlib.nullcontext()

    def register_callback(self, cb, joins):
        self.callbacks.append((cb, joins))

    def remove_callback(self, cb):
        # Mirror the real hub: removal is by equality (set.discard), not
        # identity — bound methods compare equal across re-accesses.
        self.callbacks = [(c, j) for (c, j) in self.callbacks if c != cb]


class FakeHass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        # Schedule on the running loop; the test drives it to completion.
        self.tasks.append(asyncio.ensure_future(coro))


class ToJoinBridgeTests(unittest.TestCase):
    def _bridge(self, entries):
        self.hub = FakeHub()
        return bridge.ToJoinBridge(FakeHass(), self.hub, entries)

    def test_set_join_routes_by_kind(self):
        b = self._bridge([])
        b._set_join("d5", "on")
        b._set_join("a7", "24")
        b._set_join("s9", "你好")
        self.assertEqual(self.hub.digital, {5: True})
        self.assertEqual(self.hub.analog, {7: 24})
        self.assertEqual(self.hub.serial, {9: "你好"})

    def test_set_join_drops_unknown(self):
        b = self._bridge([])
        b._set_join("a7", "unavailable")
        self.assertEqual(self.hub.analog, {})

    def test_set_join_bad_key_logs_no_write(self):
        b = self._bridge([])
        b._set_join("dabc", "1")  # must not raise
        self.assertEqual(self.hub.digital, {})

    def test_sync_all_renders_and_pushes(self):
        entries = [
            {CONF_JOIN: "a7", "value_template": Template(24)},
            {CONF_JOIN: "d5", "value_template": Template("on")},
        ]
        b = self._bridge(entries)
        b.sync_all()
        self.assertEqual(self.hub.analog, {7: 24})
        self.assertEqual(self.hub.digital, {5: True})

    def test_sync_all_isolates_failures(self):
        # A template that raises must not stop the others from syncing.
        entries = [
            {CONF_JOIN: "a7", "value_template": Template(RuntimeError("boom"))},
            {CONF_JOIN: "d5", "value_template": Template("on")},
        ]
        b = self._bridge(entries)
        b.sync_all()  # must not raise
        self.assertEqual(self.hub.digital, {5: True})
        self.assertNotIn(7, self.hub.analog)


class FromJoinBridgeTests(unittest.TestCase):
    def setUp(self):
        Script.last_instances = []
        self.hub = FakeHub()
        self.hass = FakeHass()
        entries = [{CONF_JOIN: "d5", CONF_SCRIPT: [{"x": 1}]}]
        self.bridge = bridge.FromJoinBridge(self.hass, self.hub, entries)
        self.bridge.start()
        self.script = Script.last_instances[0]

    def test_start_registers_join(self):
        self.assertEqual(len(self.hub.callbacks), 1)
        _, joins = self.hub.callbacks[0]
        self.assertEqual(joins, ["d5"])

    def _fire(self, cbtype, value):
        async def run():
            await self.bridge._join_change(cbtype, value)
            # Drain the background script task scheduled via async_create_task.
            # Each _fire gets its own loop, so the list must be emptied here or
            # the next call would await futures belonging to a closed loop.
            tasks, self.hass.tasks = self.hass.tasks, []
            if tasks:
                await asyncio.gather(*tasks)

        asyncio.run(run())

    def test_rising_edge_runs_script(self):
        self._fire("d5", "0")  # establish the low baseline
        self._fire("d5", "1")
        self.assertEqual(self.script.runs, [{"value": "1"}])

    def test_falling_edge_skipped(self):
        self._fire("d5", "0")
        self.assertEqual(self.script.runs, [])

    def test_first_report_high_does_not_run_script(self):
        """The 0xFD full sync reports every join's current level.

        Treating that as a press would replay scenes on every HA restart or
        control-system reconnect.
        """
        self._fire("d5", "1")
        self.assertEqual(self.script.runs, [])

    def test_repeated_high_runs_once(self):
        self._fire("d5", "0")
        self._fire("d5", "1")
        self._fire("d5", "1")
        self.assertEqual(self.script.runs, [{"value": "1"}])

    def test_press_release_press_runs_twice(self):
        self._fire("d5", "0")
        for _ in range(2):
            self._fire("d5", "1")
            self._fire("d5", "0")
        self.assertEqual(self.script.runs, [{"value": "1"}, {"value": "1"}])

    def test_reconnect_resync_does_not_replay(self):
        """A held-high join re-reported after a reconnect is not a new press."""
        self._fire("d5", "0")
        self._fire("d5", "1")
        self.script.runs.clear()
        self._fire("d5", "1")  # control system re-reports on reconnect
        self.assertEqual(self.script.runs, [])

    def test_stop_clears_edge_history(self):
        self._fire("d5", "0")
        self.bridge.stop()
        self._fire("d5", "1")  # first observation after reload: not an edge
        self.assertEqual(self.script.runs, [])

    def test_unconfigured_join_ignored(self):
        self._fire("d99", "1")
        self.assertEqual(self.script.runs, [])

    def test_stop_removes_callback(self):
        self.bridge.stop()
        self.assertEqual(self.hub.callbacks, [])


if __name__ == "__main__":
    unittest.main()
