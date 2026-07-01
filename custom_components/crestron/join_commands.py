"""Shared digital-join command helpers.

Kept free of Home Assistant imports so the logic can be unit tested against a
plain fake hub (only `set_digital(join, bool)` is required). `hub` here is the
low-level `CrestronXsig` instance the platform entities hold as `self._hub`.

Two patterns are factored out of the platform entities:

  - ``set_one_clear_others`` — assert exactly one digital join out of a group
    and clear the rest (climate running mode / fan speed, select option).
  - ``pulse_digital`` — a momentary digital "button press": drive a join high,
    wait a fixed time, drive it low. Each entity passes its own lock so
    concurrent presses on the same entity can't interleave.

NOTE: the dimmable light's two-step turn-off (re-assert the current analog
level → sleep → write 0) is deliberately NOT here. It is an *analog* timing
sequence, not a digital pulse, and must stay in ``light.py`` (see the comment
there) — folding it into ``pulse_digital`` would break it.
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Protocol

# Default momentary-button hold time, in seconds.
PULSE_SECONDS = 0.2


class _DigitalHub(Protocol):
    """The slice of CrestronXsig these helpers need (avoids a hard import)."""

    def set_digital(self, join: int, value: bool) -> None: ...


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
    """
    async with lock:
        hub.set_digital(join, True)
        await asyncio.sleep(seconds)
        hub.set_digital(join, False)
