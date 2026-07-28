"""Reusable XSIG join-number validators."""
import re

import voluptuous as vol

# Import straight from the codec: config validation must not depend on the
# transport layer (crestron.py owns the socket and used to re-export these).
from .xsig_protocol import ANALOG_JOIN_MAX, DIGITAL_JOIN_MAX, SERIAL_JOIN_MAX

def _join_number(kind: str, maximum: int):
    """Build a validator for a bare join number.

    ``vol.All(int, vol.Range(...))`` is an isinstance check, and in Python
    ``bool`` is a subclass of ``int`` — so a YAML ``on_join: true`` sailed
    through as a "join" and later rendered as the callback key ``"dTrue"``,
    which simply never matches anything the control system reports. Reject
    bools explicitly; everything non-int is already refused.
    """

    def validator(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise vol.Invalid(
                f"{kind} join must be a whole number 1..{maximum}; "
                f"got {value!r}"
            )
        if not 1 <= value <= maximum:
            raise vol.Invalid(
                f"{kind} join must be in 1..{maximum}; got {value}"
            )
        return value

    return validator


digital_join = _join_number("digital", DIGITAL_JOIN_MAX)
analog_join = _join_number("analog", ANALOG_JOIN_MAX)
serial_join = _join_number("serial", SERIAL_JOIN_MAX)

_JOIN_LIMITS = {
    "d": DIGITAL_JOIN_MAX,
    "a": ANALOG_JOIN_MAX,
    "s": SERIAL_JOIN_MAX,
}

# Strict: kind in {d,a,s}, number with no leading zero / sign / whitespace.
# Using int() alone is too lenient — int('1 ') == 1, int('+1') == 1.
_JOIN_KEY_RE = re.compile(r"^([das])([1-9][0-9]*)$")


def join_key(value: object) -> str:
    """Validate a `to_joins`/`from_joins` key like 'd12', 'a35', 's4'.

    Catches typos and out-of-range joins at config-load time rather than
    silently ignoring them at runtime.
    """
    if not isinstance(value, str):
        raise vol.Invalid(f"Join key must be a string; got {value!r}")
    m = _JOIN_KEY_RE.match(value)
    if not m:
        raise vol.Invalid(
            f"Join key must be '<kind><number>' where kind is d/a/s and "
            f"number has no leading zero/sign/whitespace; got {value!r}"
        )
    kind, num_str = m.group(1), m.group(2)
    num = int(num_str)
    limit = _JOIN_LIMITS[kind]
    if not 1 <= num <= limit:
        raise vol.Invalid(f"{value!r}: {kind} join must be in 1..{limit}")
    return value
