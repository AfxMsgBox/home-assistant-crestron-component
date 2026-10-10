"""Overlapping-connection takeover must reset per-connection state.

The dangerous case is a new connection arriving before the old one has
finished closing. Availability never changes value across that handover — the
new connection's _notify_available(True) is deduplicated, and the old
connection's finally sees it is no longer the active writer and stays quiet —
so nothing observable happens unless the hub signals "new connection" in its
own right.

These run a real TCP server on an ephemeral port. The final test also wires the
real FromJoinBridge to that server and observes its Script, so the connection
hook, decoder, edge detector and script scheduling are covered as one chain.
"""

import asyncio
import logging
import sys
import types
import unittest

from loader import load


def _module(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class Script:
    """Minimal HA Script stand-in that records each invocation."""

    def __init__(self, hass, sequence, name, domain):
        self.runs = []

    async def async_run(self, variables, context):
        self.runs.append(variables)


class Template:
    def __init__(self, value=None, hass=None):
        self._value = value

    def async_render(self):
        return self._value


class TrackTemplate:
    def __init__(self, template, variables):
        self.template = template


def _install_bridge_stubs():
    ha = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.event",
        TrackTemplate=TrackTemplate,
        async_track_template_result=lambda *args, **kwargs: types.SimpleNamespace(
            async_remove=lambda: None
        ),
    )
    _module("homeassistant.helpers.template", Template=Template)
    _module("homeassistant.helpers.script", Script=Script)
    _module("homeassistant.core", callback=lambda fn: fn, Context=object)
    _module(
        "homeassistant.const",
        CONF_VALUE_TEMPLATE="value_template",
        CONF_ATTRIBUTE="attribute",
        CONF_ENTITY_ID="entity_id",
    )
    ha.helpers = helpers


_install_bridge_stubs()
xsig = load("crestron")
protocol = load("xsig_protocol")
bridge = load("bridge")
logging.getLogger(xsig.__name__).setLevel(logging.CRITICAL)
logging.getLogger(bridge.__name__).setLevel(logging.CRITICAL)


async def _read_handshake(reader):
    """Consume the 0xFD the hub sends on every accepted connection."""
    return await asyncio.wait_for(reader.readexactly(1), timeout=2)


class TakeoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = xsig.CrestronXsig()
        self.connects = 0
        self.available_events = []

        async def on_connect():
            self.connects += 1

        async def cb(cbtype, value):
            if cbtype == xsig.AVAILABLE_KEY:
                self.available_events.append(value)

        self.hub.register_connect_callback(on_connect)
        self.hub.register_callback(cb, joins=["d5"])
        await self.hub.listen(0)
        self.port = self.hub._server.sockets[0].getsockname()[1]
        self.open_writers = []

    async def asyncTearDown(self):
        for writer in self.open_writers:
            try:
                writer.close()
            except Exception:
                pass
        await self.hub.stop()

    async def _connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.open_writers.append(writer)
        await _read_handshake(reader)
        return reader, writer

    async def test_overlapping_takeover_fires_connect_callback(self):
        """The signal the from_joins baseline reset hangs off of."""
        await self._connect()
        await asyncio.sleep(0.1)
        self.assertEqual(self.connects, 1)

        # Second connection while the first is still open.
        await self._connect()
        await asyncio.sleep(0.2)
        self.assertEqual(
            self.connects, 2, "takeover must be announced as a new connection"
        )

    async def test_overlapping_takeover_emits_no_availability_change(self):
        """Documents why availability cannot carry the reset."""
        await self._connect()
        await asyncio.sleep(0.1)
        self.available_events.clear()
        await self._connect()
        await asyncio.sleep(0.2)
        self.assertEqual(
            self.available_events,
            [],
            "availability is deduplicated across a takeover",
        )

    async def test_plain_reconnect_fires_connect_callback_too(self):
        _reader, writer = await self._connect()
        await asyncio.sleep(0.1)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        await asyncio.sleep(0.15)
        await self._connect()
        await asyncio.sleep(0.15)
        self.assertEqual(self.connects, 2)


class BaselineResetIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Real TCP frames -> real FromJoinBridge -> Script during a takeover."""

    async def asyncSetUp(self):
        self.hub = xsig.CrestronXsig()
        self.hass = types.SimpleNamespace(tasks=set())

        def create_task(coro):
            task = asyncio.create_task(coro)
            self.hass.tasks.add(task)
            task.add_done_callback(self.hass.tasks.discard)
            return task

        self.hass.async_create_task = create_task
        self.from_bridge = bridge.FromJoinBridge(
            self.hass,
            self.hub,
            [{"join": "d5", "script": [{"service": "test.record"}]}],
        )
        self.from_bridge.start()
        self.script = self.from_bridge._scripts["d5"]

        async def on_connect():
            self.from_bridge.reset_connection_baseline()

        self.hub.register_connect_callback(on_connect)
        await self.hub.listen(0)
        self.port = self.hub._server.sockets[0].getsockname()[1]
        self.open_writers = []

    async def asyncTearDown(self):
        self.from_bridge.stop()
        for writer in self.open_writers:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        if self.hass.tasks:
            await asyncio.gather(*tuple(self.hass.tasks), return_exceptions=True)
        await self.hub.stop()

    async def _connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.open_writers.append(writer)
        await _read_handshake(reader)
        return reader, writer

    async def _send_digital(self, writer, value):
        writer.write(protocol.encode_digital(5, value))
        await writer.drain()
        expected = bool(value)
        for _ in range(100):
            if (
                self.hub.has_digital(5)
                and self.hub.get_digital(5) is expected
            ):
                await asyncio.sleep(0)
                return
            await asyncio.sleep(0.01)
        self.fail(f"d5={int(expected)} was not processed")

    async def test_takeover_sync_is_inert_then_real_press_runs_once(self):
        _reader1, writer1 = await self._connect()
        await self._send_digital(writer1, False)
        self.assertEqual(self.script.runs, [])

        # Keep writer1 open while writer2 takes over. The first high from the
        # new connection is a full-sync baseline, not a button press.
        _reader2, writer2 = await self._connect()
        await self._send_digital(writer2, True)
        self.assertEqual(self.script.runs, [])

        # A genuine low -> high transition within writer2's connection runs
        # the configured script exactly once.
        await self._send_digital(writer2, False)
        await self._send_digital(writer2, True)
        for _ in range(100):
            if self.script.runs:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.script.runs, [{"value": "1"}])


if __name__ == "__main__":
    unittest.main()
