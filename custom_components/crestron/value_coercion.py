"""Pure helpers for coercing HA template results into XSIG join values.

Kept free of Home Assistant imports so the conversion rules can be unit
tested in isolation.
"""

_TRUE_STRINGS = {"on", "True", "true", "1"}
_FALSE_STRINGS = {"off", "False", "false", "0"}
_INVALID_STRINGS = {"unknown", "unavailable", "None", "none", ""}


def to_digital(result):
    """Coerce a template result to bool, or None if not interpretable."""
    if isinstance(result, bool):
        return result
    s = str(result)
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    return None


def to_analog(result):
    """Coerce to a 0-65535 int, or None for unknown/unavailable/garbage."""
    s = str(result)
    if s in _INVALID_STRINGS:
        return None
    try:
        value = int(float(s))
    except (TypeError, ValueError):
        return None
    return max(0, min(65535, value))


def to_serial(result):
    """Coerce to a string, or None for unknown/unavailable."""
    s = str(result)
    if s in _INVALID_STRINGS:
        return None
    return s
