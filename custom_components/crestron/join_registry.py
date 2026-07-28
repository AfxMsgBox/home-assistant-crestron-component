"""Detect joins claimed by more than one owner in the loaded YAML.

The xlsx converter already warns about duplicate joins, but only for workbooks
it generated — hand-written YAML, or a `to_joins` entry pointing at a join some
entity already drives, reaches the runtime completely unchecked. The symptom is
two things fighting over one signal with nothing in the log to explain it.

Reader/writer is the distinction that matters:

  - **Two writers** on one join is ambiguous ownership. Both entities issue
    commands on the same signal and each one's feedback reflects the other's
    actions.
  - **Duplicate `to_joins` / `from_joins` keys** are strictly worse: the bridge
    keys those by join, so the second entry silently replaces the first and one
    template (or script) never runs at all.
  - **A reader sharing a writer's join is normal** and documented: a `sensor`
    with `mode_joins` mirroring a climate's running modes, a `binary_sensor`
    watching a relay. Those must stay silent or the check is just noise.

Kept free of Home Assistant imports so it can be unit tested directly.
"""

from __future__ import annotations

from typing import Any, Iterable, NamedTuple, Optional

# Fields that address the *analog* signal space. Everything else ending in
# `_join` is digital. The two spaces are unrelated, so a1 and d1 never clash.
_ANALOG_FIELDS = frozenset({
    "brightness_join",
    "color_temp_join",
    "pos_join",
    "set_temp_join",
    "reg_temp_join",
    "volume_join",
    "source_number_join",
    "value_join",  # number (write) and sensor (read)
})

# Per platform: which join fields HA drives, and which it only observes.
# `{label: join}` maps are named here too; their values are digital joins.
_WRITE_FIELDS: dict[str, frozenset[str]] = {
    "light": frozenset({
        "brightness_join", "color_temp_join", "on_join", "off_join",
        "switch_join",
    }),
    "switch": frozenset({"switch_join", "on_join", "off_join"}),
    "climate": frozenset({
        "on_join", "off_join", "set_temp_join",
        "mode_cool_join", "mode_heat_join", "mode_fan_join", "mode_dry_join",
        "fan_low_join", "fan_med_join", "fan_high_join", "fan_auto_join",
    }),
    "cover": frozenset({"pos_join", "open_join", "close_join", "stop_join"}),
    "number": frozenset({"value_join"}),
    "select": frozenset({"options"}),
    "media_player": frozenset({
        "mute_join", "volume_join", "source_number_join",
    }),
    "binary_sensor": frozenset(),
    "sensor": frozenset(),
}
_READ_FIELDS: dict[str, frozenset[str]] = {
    "light": frozenset({"state_join"}),
    "switch": frozenset({"state_join", "mode_joins"}),
    "climate": frozenset({"reg_temp_join"}),
    "cover": frozenset({
        "is_opening_join", "is_closing_join", "is_closed_join",
    }),
    "number": frozenset(),
    "select": frozenset(),
    "media_player": frozenset(),
    "binary_sensor": frozenset({"is_on_join"}),
    "sensor": frozenset({"value_join", "mode_joins"}),
}

# Fields holding a {label: join} map rather than a bare join number.
_MAP_FIELDS = frozenset({"mode_joins", "options"})


class Usage(NamedTuple):
    """One claim on one join."""

    space: str  # "d" | "a" | "s"
    join: int
    owner: str  # human-readable "platform 'name'"
    field: str
    writes: bool

    def describe(self) -> str:
        return f"{self.owner} ({self.field}, {'write' if self.writes else 'read'})"


def _entity_usages(platform: str, config: dict[str, Any]) -> Iterable[Usage]:
    writes = _WRITE_FIELDS.get(platform, frozenset())
    reads = _READ_FIELDS.get(platform, frozenset())
    owner = f"{platform} {config.get('name', '<unnamed>')!r}"
    for field in sorted(writes | reads):
        value = config.get(field)
        if value is None:
            continue
        is_write = field in writes
        if field in _MAP_FIELDS:
            if not isinstance(value, dict):
                continue
            for join in value.values():
                if isinstance(join, int):
                    yield Usage("d", join, owner, field, is_write)
            continue
        if not isinstance(value, int):
            continue
        space = "a" if field in _ANALOG_FIELDS else "d"
        yield Usage(space, value, owner, field, is_write)


def _bridge_usages(
    entries: Any, label: str, writes: bool
) -> Iterable[Usage]:
    """Usages for a `to_joins` / `from_joins` list (keys like ``"d12"``)."""
    for index, entry in enumerate(entries or []):
        key = entry.get("join") if isinstance(entry, dict) else None
        if not isinstance(key, str) or len(key) < 2:
            continue
        space, number = key[:1], key[1:]
        if space not in ("d", "a", "s") or not number.isdigit():
            continue
        yield Usage(space, int(number), f"{label}[{index}]", "join", writes)


def collect_join_usage(
    yaml_config: dict[str, Any]
) -> dict[tuple[str, int], list[Usage]]:
    """Map every ``(space, join)`` to the list of things that claim it."""
    usage: dict[tuple[str, int], list[Usage]] = {}
    for platform in _WRITE_FIELDS:
        for config in yaml_config.get(platform, []) or []:
            if not isinstance(config, dict):
                continue
            for entry in _entity_usages(platform, config):
                usage.setdefault((entry.space, entry.join), []).append(entry)
    for entry in _bridge_usages(yaml_config.get("to_joins"), "to_joins", True):
        usage.setdefault((entry.space, entry.join), []).append(entry)
    for entry in _bridge_usages(
        yaml_config.get("from_joins"), "from_joins", False
    ):
        usage.setdefault((entry.space, entry.join), []).append(entry)
    return usage


def find_conflicts(yaml_config: dict[str, Any]) -> list[str]:
    """Return one human-readable line per genuine conflict.

    Ordered by signal space then join number so the output is stable across
    runs (dict iteration order otherwise follows YAML).
    """
    conflicts: list[str] = []
    usage = collect_join_usage(yaml_config)
    for (space, join) in sorted(usage):
        claims = usage[(space, join)]

        # A duplicated bridge key is a guaranteed silent loss, not merely
        # ambiguous: ToJoinBridge/FromJoinBridge key their maps by join.
        for label in ("to_joins", "from_joins"):
            dupes = [c for c in claims if c.owner.startswith(f"{label}[")]
            if len(dupes) > 1:
                conflicts.append(
                    f"{space}{join}: {len(dupes)} {label} entries target the "
                    f"same join ({', '.join(c.owner for c in dupes)}); only the "
                    f"last one takes effect"
                )

        writers = [c for c in claims if c.writes]
        if len(writers) > 1:
            conflicts.append(
                f"{space}{join}: driven by {len(writers)} owners — "
                + "; ".join(w.describe() for w in writers)
            )
    return conflicts


def usage_summary(yaml_config: dict[str, Any]) -> dict[str, Any]:
    """Compact snapshot for the diagnostics download."""
    usage = collect_join_usage(yaml_config)
    return {
        "joins_in_use": len(usage),
        "conflicts": find_conflicts(yaml_config),
    }
