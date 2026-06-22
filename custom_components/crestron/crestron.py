import asyncio
import struct
import logging
import time

_LOGGER = logging.getLogger(__name__)


AVAILABLE_KEY = "available"

# Initial full-sync timing probe master switch. Set to False to disable entirely
# (no background task, no stat-keeping, no summary log — near-zero overhead).
SYNC_TIMING_ENABLED = True
# Silence gap that marks the initial full-sync burst as "done": once no join
# frame has arrived for this long, log the one-shot summary.
SYNC_SETTLE_SECONDS = 1.0

# XSIG protocol join number limits.
DIGITAL_JOIN_MAX = 4096
ANALOG_JOIN_MAX = 1024
SERIAL_JOIN_MAX = 1024
SERIAL_MAX_BYTES = 252


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

    async def listen(self, port):
        """Start TCP XSIG server (non-blocking; returns after server is listening)."""
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

    async def _close_writer(self):
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _notify_available(self, available):
        if self._available == available:
            return
        self._available = available
        await self._dispatch(AVAILABLE_KEY, "True" if available else "False")

    async def handle_connection(self, reader, writer):
        """Parse packets from Crestron XSIG symbol."""
        peer = writer.get_extra_info("peername")
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

            # Timing probe: measure how long the initial full-join-sync burst
            # takes (and how much of it is HA-side dispatch) to locate startup
            # slowness. Gated by SYNC_TIMING_ENABLED; stats=None disables it.
            t_request = time.monotonic()
            stats = (
                {"frames": 0, "first": None, "last": None, "dispatch": 0.0}
                if SYNC_TIMING_ENABLED
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

            while True:
                try:
                    head = await reader.readexactly(1)
                except asyncio.IncompleteReadError:
                    break

                # Sync all joins request
                if head[0] == 0xFB:
                    _LOGGER.debug("Got update-all-joins request")
                    if self._sync_all_joins_callback is not None:
                        await self._sync_all_joins_callback()
                    continue

                try:
                    second = await reader.readexactly(1)
                except asyncio.IncompleteReadError:
                    break
                data = head + second

                # Digital Join: 10v jjjjj  0jjj jjjj  (v = inverted level)
                if (
                    data[0] & 0b11000000 == 0b10000000
                    and data[1] & 0b10000000 == 0b00000000
                ):
                    join = ((data[0] & 0b00011111) << 7 | data[1]) + 1
                    value = ~data[0] >> 5 & 0b1
                    self._digital[join] = bool(value)
                    # Lazy %-formatting: on a 1000+ join cold-start sync this
                    # parser runs thousands of times in a burst; an f-string
                    # would build the message even when debug is disabled.
                    _LOGGER.debug("Got Digital: %s = %s", join, value)
                    await self._timed_dispatch(f"d{join}", str(value), stats)

                # Analog Join: 11vv 0jjj  0jjj jjjj  0vvv vvvv  0vvv vvvv
                elif (
                    data[0] & 0b11001000 == 0b11000000
                    and data[1] & 0b10000000 == 0b00000000
                ):
                    try:
                        tail = await reader.readexactly(2)
                    except asyncio.IncompleteReadError:
                        break
                    data += tail
                    join = ((data[0] & 0b00000111) << 7 | data[1]) + 1
                    value = (
                        (data[0] & 0b00110000) << 10 | data[2] << 7 | data[3]
                    )
                    self._analog[join] = value
                    _LOGGER.debug("Got Analog: %s = %s", join, value)
                    await self._timed_dispatch(f"a{join}", str(value), stats)

                # Serial Join: 1100 1jjj  0jjj jjjj  <UTF-8>  0xFF
                elif (
                    data[0] & 0b11111000 == 0b11001000
                    and data[1] & 0b10000000 == 0b00000000
                ):
                    try:
                        body = await reader.readuntil(b"\xff")
                    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                        break
                    join = ((data[0] & 0b00000111) << 7 | data[1]) + 1
                    try:
                        string = body[:-1].decode("utf-8")
                    except UnicodeDecodeError:
                        _LOGGER.warning(f"Invalid UTF-8 on serial join {join}")
                        continue
                    self._serial[join] = string
                    _LOGGER.debug("Got String: %s = %s", join, string)
                    await self._timed_dispatch(f"s{join}", string, stats)

                else:
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug("Unknown Packet: %s", data.hex())

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
                await writer.wait_closed()
            except Exception:
                pass
            if was_active:
                await self._notify_available(False)

    def is_available(self):
        return self._available

    def get_analog(self, join):
        return self._analog.get(join, 0)

    def get_digital(self, join):
        return self._digital.get(join, False)

    def get_serial(self, join):
        return self._serial.get(join, "")

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
        value = max(0, min(65535, int(value)))
        data = struct.pack(
            ">BBBB",
            0b11000000 | (value >> 10 & 0b00110000) | (join - 1) >> 7,
            (join - 1) & 0b01111111,
            value >> 7 & 0b01111111,
            value & 0b01111111,
        )
        self._write(data)
        _LOGGER.debug("Sending Analog: %s, %s", join, value)

    def set_digital(self, join, value):
        """Send Digital Join to Crestron XSIG symbol."""
        if not 1 <= join <= DIGITAL_JOIN_MAX:
            _LOGGER.warning(
                f"Digital join {join} out of range (1..{DIGITAL_JOIN_MAX})"
            )
            return
        flag = 0b00100000 if not value else 0
        data = struct.pack(
            ">BB",
            0b10000000 | flag | ((join - 1) >> 7),
            (join - 1) & 0b01111111,
        )
        self._write(data)
        _LOGGER.debug("Sending Digital: %s, %s", join, bool(value))

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
        data = struct.pack(
            ">BB", 0b11001000 | ((join - 1) >> 7), (join - 1) & 0b01111111
        )
        data += encoded
        data += b"\xff"
        self._write(data)
        _LOGGER.debug("Sending Serial: %s, %s", join, string)
