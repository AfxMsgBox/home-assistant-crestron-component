"""Shared digital-join command and feedback helpers.

Kept free of Home Assistant imports so the logic can be unit tested against a
plain fake hub (only `set_digital`/`get_digital` are required). `hub` here is
the low-level `CrestronXsig` instance the platform entities hold as
`self._hub`.

Three patterns are factored out of the platform entities:

  - ``set_one_clear_others`` — assert exactly one digital join out of a group
    and clear the rest (climate running mode / fan speed, select option).
  - ``pulse_digital`` — a momentary digital "button press": drive a join high,
    wait a fixed time, drive it low. Each entity passes its own lock so
    concurrent presses on the same entity can't interleave.
  - ``paired_feedback`` — read a mutually-exclusive feedback pair (the control
    system reports state back on the very joins used to command it). Used by
    switch, on/off light, climate power and cover open/closed.

NOTE: the dimmable light's two-step turn-off (re-assert the current analog
level → sleep → write 0) is deliberately NOT here. It is an *analog* timing
sequence, not a digital pulse, and must stay in ``light.py`` (see the comment
there) — folding it into ``pulse_digital`` would break it.
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Optional, Protocol

# Default momentary-button hold time, in seconds.
PULSE_SECONDS = 0.2


class _DigitalHub(Protocol):
    """The slice of CrestronXsig these helpers need (avoids a hard import)."""

    def set_digital(self, join: int, value: bool) -> None: ...

    def get_digital(self, join: int) -> bool: ...


def paired_feedback(
    hub: _DigitalHub, true_join: Optional[int], false_join: Optional[int]
) -> Optional[bool]:
    """Read a mutually-exclusive feedback pair; ``None`` means indeterminate.

    The control system reports state back on the same two joins that command
    it: one high for "true" (on / open), the other high for "false" (off /
    closed). Exactly one high is the only definitive reading —

      - both low  -> not reported yet (it pushes on change only), or mid-travel
      - both high -> a momentary transition caught between scans

    both of which must leave the caller's current (restored or optimistic)
    state alone rather than flipping it. A missing join reads as low, which
    keeps a half-configured pair usable: whichever join *is* configured still
    gives a definitive answer while it is high.
    """
    true_level = true_join is not None and hub.get_digital(true_join)
    false_level = false_join is not None and hub.get_digital(false_join)
    if true_level != false_level:
        return bool(true_level)
    return None


def set_one_clear_others(
    hub: _DigitalHub, joins: Iterable[int], target: int
) -> None:
    """Assert ``target`` and clear every other join in ``joins``.

    ``joins`` is any iterable of digital join numbers; ``target`` should be one
    of them (it is set last, so passing a target outside ``joins`` simply sets
    it in addition to clearing the group). Clearing happens before the set, so
    no observer ever sees two joins high at once.
    """
    for join in joins:
        if join != target:
            hub.set_digital(join, False)
    hub.set_digital(target, True)


async def pulse_digital(
    hub: _DigitalHub,
    lock: asyncio.Lock,
    join: int,
    seconds: float = PULSE_SECONDS,
) -> None:
    """Momentary press: drive ``join`` high, hold ``seconds``, drive it low.

    ``lock`` is an ``asyncio.Lock`` owned by the calling entity so that two
    overlapping presses on the same entity serialise instead of stomping each
    other (the second press waits for the first to release the join).

    The release is in a ``finally``: a reload, a shutdown or any other task
    cancellation landing inside the hold window would otherwise leave the join
    driven high forever — a relay stuck energised, a cover motor still being
    told to run. Releasing on the way out costs one frame and cannot make
    things worse (writing to a closed connection is already a no-op).
    """
    async with lock:
        hub.set_digital(join, True)
        try:
            await asyncio.sleep(seconds)
        finally:
            hub.set_digital(join, False)
