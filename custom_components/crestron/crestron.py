import asyncio
import logging
import time
from contextlib import contextmanager

from .xsig_protocol import (
    FrameDecoder,
    ProtocolError,
    encode_analog,
    encode_digital,
    encode_serial,
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
    def __init__(self, join_metadata=None):
        self._digital = {}
        self._analog = {}
        self._serial = {}
        self._join_metadata = join_metadata or {}
        self._writer = None
        self._write_batch = None  # list of frames while batching, else None
        self._join_callbacks = {}
        self._server = None
        self._available = False
        self._sync_all_joins_callback = None
        self._connect_callback = None
        self._port = None
        self._peer = None  # str repr of the most-recent connection's peername

    def describe_join_key(self, key):
        """Return a log label with configured device/entity and field meaning."""
        descriptions = self._join_metadata.get(key)
        if not descriptions:
            return f"{key} [not configured in YAML]"
        return f"{key} [" + " | ".join(descriptions) + "]"

    def describe_join(self, space, join):
        """Describe a numbered join in one of the d/a/s signal spaces."""
        return self.describe_join_key(f"{space}{join}")

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

    def register_connect_callback(self, callback):
        """Register a coroutine to run once a control system has connected.

        The handshake is otherwise one-way: we ask the control system to report
        everything (0xFD) but never volunteer our own side. Without this hook a
        reconnect leaves every ``to_join`` at whatever value the control system
        last received, so panel feedback can sit stale indefinitely — the
        protocol has no "push me your outputs" request in that direction.
        """
        _LOGGER.debug("Connect callback registered")
        self._connect_callback = callback

    def register_callback(self, callback, joins):
        """Register ``callback`` for the given join keys (e.g. ``["d5", "a3"]``).

        Availability changes are always delivered too, so every subscriber can
        react to the connection going up or down without asking for it.
        """
        for key in joins:
            self._join_callbacks.setdefault(key, set()).add(callback)
        self._join_callbacks.setdefault(AVAILABLE_KEY, set()).add(callback)

    def remove_callback(self, callback):
        for callbacks in self._join_callbacks.values():
            callbacks.discard(callback)

    async def _safe_call(self, cb, cbtype, value):
        """Isolate per-callback failures so one bad subscriber can't kill the
        TCP read loop or take down the whole XSIG session."""
        try:
            await cb(cbtype, value)
        except Exception:
            _LOGGER.exception(
                "Crestron callback %r raised on %s=%r",
                cb,
                (
                    cbtype
                    if cbtype == AVAILABLE_KEY
                    else self.describe_join_key(cbtype)
                ),
                value,
            )

    async def _dispatch(self, cbtype, value):
        # Snapshot the callback set before iterating: each callback is awaited,
        # and during those awaits another task (e.g. an entity being added or
        # removed) may register/remove callbacks and mutate this set, which
        # would otherwise raise "Set changed size during iteration".
        for cb in tuple(self._join_callbacks.get(cbtype, ())):
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
        # Distinct joins, not frames: the control system can report the same
        # join several times in one sync, so "332 frames" was being logged as
        # "332 joins" and read as a join count.
        stats["joins"].add(cbtype)
        await self._dispatch(cbtype, value)
        stats["last"] = time.monotonic()
        stats["dispatch"] += stats["last"] - now

    async def _handle_frame(self, frame, stats):
        """Apply one decoded inbound frame: update cache + dispatch / log."""
        kind = frame.kind
        if kind == "digital":
            self._digital[frame.join] = bool(frame.value)
            if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
                _FRAME_LOGGER.debug(
                    "Got Digital: %s = %s",
                    self.describe_join("d", frame.join),
                    frame.value,
                )
            await self._timed_dispatch(f"d{frame.join}", str(frame.value), stats)
        elif kind == "analog":
            self._analog[frame.join] = frame.value
            if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
                _FRAME_LOGGER.debug(
                    "Got Analog: %s = %s",
                    self.describe_join("a", frame.join),
                    frame.value,
                )
            await self._timed_dispatch(f"a{frame.join}", str(frame.value), stats)
        elif kind == "serial":
            self._serial[frame.join] = frame.value
            if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
                _FRAME_LOGGER.debug(
                    "Got String: %s = %s",
                    self.describe_join("s", frame.join),
                    frame.value,
                )
            await self._timed_dispatch(f"s{frame.join}", frame.value, stats)
        elif kind == "sync_all":
            _LOGGER.debug("Got update-all-joins request")
            if self._sync_all_joins_callback is not None:
                await self._sync_all_joins_callback()
        elif kind == "bad_utf8":
            _LOGGER.warning(
                "Invalid UTF-8 on serial join %s",
                self.describe_join("s", frame.join),
            )
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
            # Stamped here, not after the connect callback below: the metric is
            # "how long until the control system answered our 0xFD", and the
            # callback (a full to_joins render) can itself take a while. Taking
            # the timestamp afterwards subtracted that from the measurement and
            # could report 0.00s.
            t_request = time.monotonic()
            await self._notify_available(True)

            # Push our side of the state out too. 0xFD only asks the control
            # system to report *its* joins; without this, to_joins keep whatever
            # value the control system last saw until a template happens to
            # change or it asks for a resync (0xFB). Isolated so a bad template
            # can't take down a connection that is otherwise fine.
            if self._connect_callback is not None:
                try:
                    await self._connect_callback()
                except Exception:
                    _LOGGER.exception("Connect callback failed")

            # Timing probe for the initial full-join-sync burst. It follows the
            # normal integration logger: enabled at info/debug, disabled at
            # warning+.
            stats = (
                {
                    "frames": 0,
                    "joins": set(),
                    "first": None,
                    "last": None,
                    "dispatch": 0.0,
                }
                if _sync_timing_enabled()
                else None
            )

            async def _log_sync_when_settled():
                while True:
                    await asyncio.sleep(SYNC_SETTLE_SECONDS)
                    # Gate on "last", not "first": _timed_dispatch sets "first"
                    # *before* awaiting the dispatch and "last" only after it
                    # returns, so a first frame whose dispatch outlives this
                    # sleep (hundreds of entities writing state on a cold
                    # start) would otherwise land us on `monotonic() - None`.
                    # "last" being set implies "first" is too.
                    if stats["last"] is not None and (
                        time.monotonic() - stats["last"] >= SYNC_SETTLE_SECONDS
                    ):
                        _LOGGER.info(
                            "Initial join sync settled: %d frames covering %d "
                            "joins, span %.2fs "
                            "(HA dispatch %.2fs of that; first frame %.2fs after 0xFD)",
                            stats["frames"],
                            len(stats["joins"]),
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

    def diagnostics(self, redact=True):
        """Snapshot of connection + cache state for the diagnostics download.

        Read-only; safe to call any time. The join caches can be large (1000+
        entries on a full sync), so this returns their sizes plus the contents
        keyed by join — the diagnostics platform decides how to render.

        ``redact`` (the default) withholds two things, because the entire point
        of this download is to hand it to someone else:

          - **serial join contents**, which carry free text — door-access names,
            calendar entries, whatever the panel displays. The join numbers and
            value lengths are kept, which is what "has this join ever been
            reported?" actually needs.
          - **the control system's address**, which is site information.

        Digital and analog values are just levels and numbers, so they stay.
        """
        peer = self._peer
        serial = {join: value for join, value in self._serial.items()}
        if redact:
            peer = "**REDACTED**" if peer is not None else None
            serial = {
                join: f"<{len(value)} chars redacted>"
                for join, value in self._serial.items()
            }
        return {
            "available": self._available,
            "listening_port": self._port,
            "connected": self._writer is not None,
            "peer": peer,
            "cache_counts": {
                "digital": len(self._digital),
                "analog": len(self._analog),
                "serial": len(self._serial),
            },
            "digital": dict(self._digital),
            "analog": dict(self._analog),
            "serial": serial,
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

    @contextmanager
    def batched_writes(self):
        """Coalesce every ``set_*`` made inside the block into one socket write.

        A full ``to_joins`` resync renders hundreds of joins back to back; one
        ``writer.write()`` per join means hundreds of small buffer appends (and,
        on the control system's side, potentially hundreds of tiny segments).
        The frames are independent and order-preserving, so concatenating them
        is equivalent on the wire and strictly cheaper.

        Reentrant-safe: a nested block joins the outermost batch. The batch is
        flushed even if the body raises, so a mid-sync failure still delivers
        the joins that were already rendered.
        """
        if self._write_batch is not None:
            yield  # already batching; the outermost block owns the flush
            return
        self._write_batch = []
        try:
            yield
        finally:
            batch = self._write_batch
            self._write_batch = None
            if batch:
                self._write(b"".join(batch))

    def _write(self, data):
        if self._write_batch is not None:
            self._write_batch.append(data)
            return
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
                "Analog join %s out of range (1..%d)",
                self.describe_join("a", join),
                ANALOG_JOIN_MAX,
            )
            return
        self._write(encode_analog(join, value))
        if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
            _FRAME_LOGGER.debug(
                "Sending Analog: %s = %s",
                self.describe_join("a", join),
                value,
            )

    def set_digital(self, join, value):
        """Send Digital Join to Crestron XSIG symbol."""
        if not 1 <= join <= DIGITAL_JOIN_MAX:
            _LOGGER.warning(
                "Digital join %s out of range (1..%d)",
                self.describe_join("d", join),
                DIGITAL_JOIN_MAX,
            )
            return
        self._write(encode_digital(join, value))
        if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
            _FRAME_LOGGER.debug(
                "Sending Digital: %s = %s",
                self.describe_join("d", join),
                bool(value),
            )

    def set_serial(self, join, string):
        """Send String Join to Crestron XSIG symbol."""
        if not 1 <= join <= SERIAL_JOIN_MAX:
            _LOGGER.warning(
                "Serial join %s out of range (1..%d)",
                self.describe_join("s", join),
                SERIAL_JOIN_MAX,
            )
            return
        encoded = string.encode("utf-8")
        if len(encoded) > SERIAL_MAX_BYTES:
            _LOGGER.info(
                "Could not send %s. String too long (%d bytes > %d)",
                self.describe_join("s", join),
                len(encoded),
                SERIAL_MAX_BYTES,
            )
            return
        self._write(encode_serial(join, encoded))
        if _FRAME_LOGGER.isEnabledFor(logging.DEBUG):
            _FRAME_LOGGER.debug(
                "Sending Serial: %s = %s",
                self.describe_join("s", join),
                string,
            )
