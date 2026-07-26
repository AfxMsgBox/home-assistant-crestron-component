"""Reusable XSIG join-number validators."""
import re

import voluptuous as vol

from .crestron import ANALOG_JOIN_MAX, DIGITAL_JOIN_MAX, SERIAL_JOIN_MAX

digital_join = vol.All(int, vol.Range(min=1, max=DIGITAL_JOIN_MAX))
analog_join = vol.All(int, vol.Range(min=1, max=ANALOG_JOIN_MAX))
serial_join = vol.All(int, vol.Range(min=1, max=SERIAL_JOIN_MAX))

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
