"""Pure XSIG wire-format codec (no asyncio, no Home Assistant, no sockets).

Splitting the frame encode/decode out of ``CrestronXsig`` lets the protocol be
unit-tested without standing up a TCP server, and makes the tricky edge cases
(frames split across reads, invalid UTF-8, unknown frames) easy to cover.

Wire formats (first byte's high bits select the type):
  - Digital: ``10v jjjjj  0jjj jjjj``            (2 bytes; v = inverted level)
  - Analog:  ``11vv 0jjj  0jjj jjjj  0vvvvvvv 0vvvvvvv``  (4 bytes)
  - Serial:  ``1100 1jjj  0jjj jjjj  <UTF-8…>  0xFF``     (variable)
  - Control: ``0xFB`` = "resend all joins"; ``0xFD`` (sent by us) = "report all".

``CrestronXsig`` owns the socket, the join cache and callback dispatch; it feeds
inbound bytes to ``FrameDecoder`` and uses the ``encode_*`` helpers for output.
"""

from __future__ import annotations

import struct
from typing import NamedTuple, Optional, Union

# XSIG protocol join-number limits (also imported by schema.py for validation).
DIGITAL_JOIN_MAX = 4096
ANALOG_JOIN_MAX = 1024
SERIAL_JOIN_MAX = 1024
SERIAL_MAX_BYTES = 252

# Control byte sent by Crestron to ask HA to resend every "to" join.
SYNC_ALL_REQUEST = 0xFB

# Give up (and drop the connection) if a serial frame's 0xFF terminator hasn't
# arrived within this many buffered bytes — mirrors asyncio's default
# StreamReader limit, so a runaway/garbled stream can't grow the buffer forever.
SERIAL_SCAN_LIMIT = 65536


class ProtocolError(Exception):
    """Unrecoverable decode error; the caller should drop the connection."""


class Frame(NamedTuple):
    """A decoded inbound frame. ``kind`` is one of:

    - ``"digital"``  -> join:int, value:int (0/1)
    - ``"analog"``   -> join:int, value:int (0..65535)
    - ``"serial"``   -> join:int, value:str
    - ``"sync_all"`` -> join None, value None
    - ``"bad_utf8"`` -> join:int, value None  (serial body wasn't valid UTF-8)
    - ``"unknown"``  -> join None, value:str  (hex of the 2 bytes consumed)
    """

    kind: str
    join: Optional[int]
    value: Union[int, str, None]


# --- Encoders (output) ----------------------------------------------------

def encode_digital(join: int, value: object) -> bytes:
    """Serialize a digital join set. ``join`` assumed in range (caller checks)."""
    flag = 0b00100000 if not value else 0
    return struct.pack(
        ">BB",
        0b10000000 | flag | ((join - 1) >> 7),
        (join - 1) & 0b01111111,
    )


def encode_analog(join: int, value: int) -> bytes:
    """Serialize an analog join set; ``value`` is clamped to 0..65535."""
    value = max(0, min(65535, int(value)))
    return struct.pack(
        ">BBBB",
        0b11000000 | (value >> 10 & 0b00110000) | (join - 1) >> 7,
        (join - 1) & 0b01111111,
        value >> 7 & 0b01111111,
        value & 0b01111111,
    )


def encode_serial(join: int, encoded: bytes) -> bytes:
    """Serialize a serial join set from already-UTF-8-encoded ``encoded`` bytes.

    The caller encodes once (to enforce the byte-length limit) and passes the
    bytes in, so this helper doesn't re-encode.
    """
    header = struct.pack(
        ">BB", 0b11001000 | ((join - 1) >> 7), (join - 1) & 0b01111111
    )
    return header + encoded + b"\xff"


# --- Decoder (input) ------------------------------------------------------

class FrameDecoder:
    """Incremental parser: feed arbitrary byte chunks, get whole frames back.

    Stateful across calls — a frame split across several ``feed`` calls (TCP
    coalescing/fragmentation) is reassembled. Bytes that don't yet form a
    complete frame are retained until the rest arrives.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Append ``data`` and return the list of frames now fully available."""
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            frame, consumed = self._try_parse()
            if consumed == 0:
                break  # need more bytes
            del self._buf[:consumed]
            if frame is not None:
                frames.append(frame)
        return frames

    def _try_parse(self) -> tuple[Optional[Frame], int]:
        """Parse one frame from the front of the buffer.

        Returns ``(frame, consumed)``. ``consumed == 0`` means "not enough bytes
        yet, leave the buffer alone". ``frame`` is None only paired with
        ``consumed == 0``.
        """
        buf = self._buf
        if not buf:
            return None, 0

        # Control: resend-all-joins request (single byte).
        if buf[0] == SYNC_ALL_REQUEST:
            return Frame("sync_all", None, None), 1

        if len(buf) < 2:
            return None, 0
        b0, b1 = buf[0], buf[1]

        # Digital: 10v jjjjj  0jjj jjjj
        if b0 & 0b11000000 == 0b10000000 and b1 & 0b10000000 == 0:
            join = ((b0 & 0b00011111) << 7 | b1) + 1
            value = ~b0 >> 5 & 0b1
            return Frame("digital", join, value), 2

        # Analog: 11vv 0jjj  0jjj jjjj  0vvvvvvv 0vvvvvvv
        if b0 & 0b11001000 == 0b11000000 and b1 & 0b10000000 == 0:
            if len(buf) < 4:
                return None, 0
            join = ((b0 & 0b00000111) << 7 | b1) + 1
            value = (b0 & 0b00110000) << 10 | buf[2] << 7 | buf[3]
            return Frame("analog", join, value), 4

        # Serial: 1100 1jjj  0jjj jjjj  <UTF-8>  0xFF
        if b0 & 0b11111000 == 0b11001000 and b1 & 0b10000000 == 0:
            idx = buf.find(0xFF, 2)
            if idx == -1:
                if len(buf) > SERIAL_SCAN_LIMIT:
                    raise ProtocolError("serial frame missing 0xFF terminator")
                return None, 0  # terminator not here yet
            join = ((b0 & 0b00000111) << 7 | b1) + 1
            body = bytes(buf[2:idx])
            try:
                string = body.decode("utf-8")
            except UnicodeDecodeError:
                return Frame("bad_utf8", join, None), idx + 1
            return Frame("serial", join, string), idx + 1

        # Unrecognized: consume the 2 header bytes (matches the old loop).
        return Frame("unknown", None, bytes(buf[:2]).hex()), 2
