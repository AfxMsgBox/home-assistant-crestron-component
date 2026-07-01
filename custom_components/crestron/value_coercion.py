"""Pure helpers for coercing HA template results into XSIG join values.

Kept free of Home Assistant imports so the conversion rules can be unit
tested in isolation.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Union

_TRUE_STRINGS = {"on", "True", "true", "1"}
_FALSE_STRINGS = {"off", "False", "false", "0"}
_INVALID_STRINGS = {"unknown", "unavailable", "None", "none", ""}


def to_digital(result: Any) -> Optional[bool]:
    """Coerce a template result to bool, or None if not interpretable."""
    if isinstance(result, bool):
        return result
    s = str(result)
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    return None


def to_analog(result: Any) -> Optional[int]:
    """Coerce to a 0-65535 int, or None for unknown/unavailable/garbage."""
    s = str(result)
    if s in _INVALID_STRINGS:
        return None
    try:
        value = int(float(s))
    except (TypeError, ValueError):
        return None
    return max(0, min(65535, value))


def to_serial(result: Any) -> Optional[str]:
    """Coerce to a string, or None for unknown/unavailable."""
    s = str(result)
    if s in _INVALID_STRINGS:
        return None
    return s


# Join kind -> coercion function, for resolve_join_write below.
_COERCE_BY_KIND: dict[str, Callable[[Any], Optional[Union[bool, int, str]]]] = {
    "d": to_digital,
    "a": to_analog,
    "s": to_serial,
}


def resolve_join_write(
    key: str, result: Any
) -> Optional[tuple[str, int, Union[bool, int, str]]]:
    """Parse a join key (e.g. ``"d12"``) and coerce a template result.

    Returns ``(kind, number, value)`` ready to hand to the matching
    ``hub.set_<kind>`` setter, or ``None`` when the value is unknown/garbage
    (nothing should be written). Raises ``ValueError`` if the key's number part
    isn't an integer, so the caller can log the misconfigured key distinctly.

    Pure (no Home Assistant imports) so the to_joins write path can be unit
    tested against a fake hub.
    """
    kind = key[:1]
    coerce = _COERCE_BY_KIND.get(kind)
    if coerce is None:
        return None
    number = int(key[1:])  # ValueError on a malformed key -> caller logs
    value = coerce(result)
    if value is None:
        return None
    return (kind, number, value)
