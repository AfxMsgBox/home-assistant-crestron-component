import asyncio
import logging
import time

from .xsig_protocol import (
    FrameDecoder,
    ProtocolError,
    encode_analog,
    encode_digital,
    encode_serial,
    # Re-exported so schema.py and existing importers keep working unchanged.
    DIGITAL_JOIN_MAX,
    ANALOG_JOIN_MAX,
    SERIAL_JOIN_MAX,
    SERIAL_MAX_BYTES,
)

_LOGGER = logging.getLogger(__name__)
_FRAME_LOGGER = logging.getLogger(__name__ + ".frames")


AVAILABLE_KEY = "available"

# Silence gap that marks the initial full-sync burst as "done": once no join
# frame has arrived for this long, log the one-shot summary.
SYNC_SETTLE_SECONDS = 1.0

# Chunk size for inbound socket reads; frames are reassembled by FrameDecoder.
_READ_CHUNK = 4096
_WRITER_CLOSE_TIMEOUT = 2.0


def _sync_timing_enabled():
    return _LOGGER.isEnabledFor(logging.INFO)


class CrestronXsig:
    def __init__(self):
        self._digital = {}
        self._analog = {}
        self._serial = {}
        self._writer = None
        self._broadcast_callbacks = set()
        self._join_callbacks = {}
        self._server = None
        self._available = False
        self._sync_all_joins_callback = None
        self._port = None
        self._peer = None  # str repr of the most-recent connection's peername

    async def listen(self, port):
        """Start TCP XSIG server (non-blocking; returns after server is listening)."""
        self._port = port
        self._server = await asyncio.start_server(
            self.handle_connection, "0.0.0.0", port
        )
        addr = self._server.sockets[0].getsockname()
        _LOGGER.info(f"Listening on {addr}")

    async def stop(self):
        """Stop TCP XSIG server."""
        self._available = False
        await self._dispatch(AVAILABLE_KEY, "False")
        await self._close_writer()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        _LOGGER.info("XSIG server stopped")

    def register_sync_all_joins_callback(self, callback):
        _LOGGER.debug("Sync-all-joins callback registered")
        self._sync_all_joins_callback = callback

    def register_callback(self, callback, joins=None):
        """Register a callback.

        If ``joins`` is None, the callback receives every join event plus
        availability changes (broadcast). If a list of join keys is given,
        the callback is only invoked for those joins and for availability
        changes.
        """
        if joins is None:
            self._broadcast_callbacks.add(callback)
            return
        for key in joins:
            self._join_callbacks.setdefault(key, set()).add(callback)
        self._join_callbacks.setdefault(AVAILABLE_KEY, set()).add(callback)

    def remove_callback(self, callback):
        self._broadcast_callbacks.discard(callback)
        for callbacks in self._join_callbacks.values():
            callbacks.discard(callback)

    async def _safe_call(self, cb, cbtype, value):
        """Isolate per-callback failures so one bad subscriber can't kill the
        TCP read loop or take down the whole XSIG session."""
        try:
            await cb(cbtype, value)
        except Exception:
            _LOGGER.exception(
                f"Crestron callback {cb!r} raised on {cbtype}={value!r}"
            )

    async def _dispatch(self, cbtype, value):
        # Snapshot the callback sets before iterating: each callback is awaited,
        # and during those awaits another task (e.g. an entity being added or
        # removed) may register/remove callbacks and mutate these sets, which
        # would otherwise raise "Set changed size during iteration".
        join_targets = tuple(self._join_callbacks.get(cbtype, ()))
        if not self._broadcast_callbacks:
            for cb in join_targets:
                await self._safe_call(cb, cbtype, value)
            return
        # Deduplicate when a callback registered both broadcast and per-join.
        seen = set()
        for cb in join_targets:
            seen.add(cb)
            await self._safe_call(cb, cbtype, value)
        for cb in tuple(self._broadcast_callbacks):
            if cb not in seen:
                await self._safe_call(cb, cbtype, value)

    async def _timed_dispatch(self, cbtype, value, stats):
        """Dispatch a join event, optionally accumulating timing stats.

        ``stats`` is None when the timing probe is disabled, in which case this
        is just a thin pass-through to _dispatch (no extra bookkeeping).
        """
        if stats is None:
            await self._dispatch(cbtype, value)
            return
        now = time.monotonic()
        if stats["first"] is None:
            stats["first"] = now
        stats["frames"] += 1
        await self._dispatch(cbtype, value)
        stats["last"] = time.monotonic()
        stats["dispatch"] += stats["last"] - now

    async def _handle_frame(self, frame, stats):
        """Apply one decoded inbound frame: update cache + dispatch / log."""
        kind = frame.kind
        if kind == "digital":
            self._digital[frame.join] = bool(frame.value)
            # Lazy %-formatting: on a 1000+ join cold-start sync this runs
            # thousands of times in a burst; an f-string would build the
            # message even when debug is disabled.
            _FRAME_LOGGER.debug("Got Digital: %s = %s", frame.join, frame.value)
            await self._timed_dispatch(f"d{frame.join}", str(frame.value), stats)
        elif kind == "analog":
            self._analog[frame.join] = frame.value
            _FRAME_LOGGER.debug("Got Analog: %s = %s", frame.join, frame.value)
            await self._timed_dispatch(f"a{frame.join}", str(frame.value), stats)
        elif kind == "serial":
            self._serial[frame.join] = frame.value
            _FRAME_LOGGER.debug("Got String: %s = %s", frame.join, frame.value)
            await self._timed_dispatch(f"s{frame.join}", frame.value, stats)
        elif kind == "sync_all":
            _LOGGER.debug("Got update-all-joins request")
            if self._sync_all_joins_callback is not None:
                await self._sync_all_joins_callback()
        elif kind == "bad_utf8":
            _LOGGER.warning(f"Invalid UTF-8 on serial join {frame.join}")
        elif kind == "unknown":
            if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
                _FRAME_LOGGER.debug("Unknown Packet: %s", frame.value)

    async def _wait_writer_closed(self, writer):
        try:
            await asyncio.wait_for(
                writer.wait_closed(), timeout=_WRITER_CLOSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("Timed out waiting for XSIG writer to close")
        except Exception:
            pass

    async def _close_writer(self):
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
        except Exception:
            pass
        await self._wait_writer_closed(writer)

    async def _notify_available(self, available):
        if self._available == available:
            return
        self._available = available
        await self._dispatch(AVAILABLE_KEY, "True" if available else "False")

    async def handle_connection(self, reader, writer):
        """Parse packets from Crestron XSIG symbol."""
        peer = writer.get_extra_info("peername")
        self._peer = str(peer)
        _LOGGER.info(f"Control system connection from {peer}")
        if self._writer is not None:
            _LOGGER.warning("Existing XSIG connection still open; replacing it")
            await self._close_writer()
        self._writer = writer
        settle_task = None
        try:
            writer.write(b"\xfd")
            await writer.drain()
            await self._notify_available(True)

            # Timing probe for the initial full-join-sync burst. It follows the
            # normal integration logger: enabled at info/debug, disabled at
            # warning+.
            t_request = time.monotonic()
            stats = (
                {"frames": 0, "first": None, "last": None, "dispatch": 0.0}
                if _sync_timing_enabled()
                else None
            )

            async def _log_sync_when_settled():
                while True:
                    await asyncio.sleep(SYNC_SETTLE_SECONDS)
                    if stats["first"] is not None and (
                        time.monotonic() - stats["last"] >= SYNC_SETTLE_SECONDS
                    ):
                        _LOGGER.info(
                            "Initial join sync settled: %d joins, span %.2fs "
                            "(HA dispatch %.2fs of that; first frame %.2fs after 0xFD)",
                            stats["frames"],
                            stats["last"] - stats["first"],
                            stats["dispatch"],
                            stats["first"] - t_request,
                        )
                        return

            if stats is not None:
                settle_task = asyncio.create_task(_log_sync_when_settled())

            decoder = FrameDecoder()
            while True:
                try:
                    chunk = await reader.read(_READ_CHUNK)
                except asyncio.IncompleteReadError:
                    break
                if not chunk:  # EOF: peer closed
                    break
                for frame in decoder.feed(chunk):
                    await self._handle_frame(frame, stats)

        except ProtocolError as exc:
            _LOGGER.warning(f"XSIG protocol error: {exc}")
        except Exception as exc:
            _LOGGER.warning(f"XSIG connection error: {exc}")
        finally:
            if settle_task is not None and not settle_task.done():
                settle_task.cancel()
            _LOGGER.info(f"Control system disconnected: {peer}")
            # Only the currently active connection may clear writer state and
            # broadcast unavailability. If a newer connection has already taken
            # over (self._writer points elsewhere, or is None mid-handoff), let
            # it own availability.
            was_active = self._writer is writer
            if was_active:
                self._writer = None
            try:
                writer.close()
            except Exception:
                pass
            await self._wait_writer_closed(writer)
            if was_active:
                await self._notify_available(False)

    def is_available(self):
        return self._available

    def diagnostics(self):
        """Snapshot of connection + cache state for the diagnostics download.

        Read-only; safe to call any time. The join caches can be large (1000+
        entries on a full sync), so this returns their sizes plus the full
        contents keyed by join — the diagnostics platform decides how to render.
        """
        return {
            "available": self._available,
            "listening_port": self._port,
            "connected": self._writer is not None,
            "peer": self._peer,
            "cache_counts": {
                "digital": len(self._digital),
                "analog": len(self._analog),
                "serial": len(self._serial),
            },
            "digital": dict(self._digital),
            "analog": dict(self._analog),
            "serial": dict(self._serial),
        }

    # State getters. The control system pushes state on change only; until a
    # join has been reported it is *unknown*, which the bare getters can't
    # distinguish from a real 0/False/"". The ``has_*`` predicates and the
    # ``default`` parameter let callers handle "not yet known" explicitly.
    # The getter defaults preserve the historical 0/False/"" behaviour so
    # existing callers are unaffected; pass ``default=None`` to opt in.

    def has_analog(self, join):
        return join in self._analog

    def get_analog(self, join, default=0):
        return self._analog.get(join, default)

    def has_digital(self, join):
        return join in self._digital

    def get_digital(self, join, default=False):
        return self._digital.get(join, default)

    def has_serial(self, join):
        return join in self._serial

    def get_serial(self, join, default=""):
        return self._serial.get(join, default)

    def _write(self, data):
        if self._writer is None:
            _LOGGER.info("Could not send. No connection to hub")
            return
        try:
            self._writer.write(data)
        except Exception as exc:
            _LOGGER.warning(f"Write failed: {exc}")

    def set_analog(self, join, value):
        """Send Analog Join to Crestron XSIG symbol."""
        if not 1 <= join <= ANALOG_JOIN_MAX:
            _LOGGER.warning(
                f"Analog join {join} out of range (1..{ANALOG_JOIN_MAX})"
            )
            return
        self._write(encode_analog(join, value))
        _FRAME_LOGGER.debug("Sending Analog: %s, %s", join, value)

    def set_digital(self, join, value):
        """Send Digital Join to Crestron XSIG symbol."""
        if not 1 <= join <= DIGITAL_JOIN_MAX:
            _LOGGER.warning(
                f"Digital join {join} out of range (1..{DIGITAL_JOIN_MAX})"
            )
            return
        self._write(encode_digital(join, value))
        _FRAME_LOGGER.debug("Sending Digital: %s, %s", join, bool(value))

    def set_serial(self, join, string):
        """Send String Join to Crestron XSIG symbol."""
        if not 1 <= join <= SERIAL_JOIN_MAX:
            _LOGGER.warning(
                f"Serial join {join} out of range (1..{SERIAL_JOIN_MAX})"
            )
            return
        encoded = string.encode("utf-8")
        if len(encoded) > SERIAL_MAX_BYTES:
            _LOGGER.info(
                f"Could not send. String too long ({len(encoded)} bytes > "
                f"{SERIAL_MAX_BYTES})"
            )
            return
        self._write(encode_serial(join, encoded))
        _FRAME_LOGGER.debug("Sending Serial: %s, %s", join, string)
