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

from typing import Any, Iterable, NamedTuple

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

_FIELD_MEANINGS = {
    "brightness_join": "灯光亮度",
    "color_temp_join": "灯光色温",
    "on_join": "开启控制/反馈",
    "off_join": "关闭控制/反馈",
    "switch_join": "开关控制/反馈",
    "state_join": "开关状态反馈",
    "open_join": "打开控制",
    "close_join": "关闭控制",
    "stop_join": "停止控制",
    "pos_join": "位置控制/反馈",
    "is_opening_join": "正在打开反馈",
    "is_closing_join": "正在关闭反馈",
    "is_closed_join": "已关闭反馈",
    "set_temp_join": "目标温度",
    "reg_temp_join": "当前温度",
    "mode_cool_join": "制冷模式",
    "mode_heat_join": "制热模式",
    "mode_fan_join": "通风模式",
    "mode_dry_join": "除湿模式",
    "fan_low_join": "低速风",
    "fan_med_join": "中速风",
    "fan_high_join": "高速风",
    "fan_auto_join": "自动风速",
    "mute_join": "静音",
    "volume_join": "音量",
    "source_number_join": "音源编号/开关机",
    "is_on_join": "开关状态反馈",
    "to_joins": "HA 状态下发到快思聪",
    "from_joins": "快思聪触发 HA 脚本",
}

_PLATFORM_VALUE_MEANINGS = {
    "number": "可读写数值",
    "sensor": "传感器数值",
}


def _field_meaning(platform: str, field: str) -> str:
    """Human meaning for one YAML join field."""
    if field.startswith("mode_joins["):
        return f"模式反馈 {field[len('mode_joins['):-1]}"
    if field.startswith("options["):
        return f"选项 {field[len('options['):-1]}"
    if field == "value_join":
        return _PLATFORM_VALUE_MEANINGS.get(platform, "数值")
    return _FIELD_MEANINGS.get(field, field)


class Usage(NamedTuple):
    """One claim on one join."""

    space: str  # "d" | "a" | "s"
    join: int
    owner: str  # human-readable "platform 'name'"
    field: str
    meaning: str
    writes: bool

    def describe(self) -> str:
        direction = "control/write" if self.writes else "feedback/read"
        return (
            f"{self.owner}: {self.meaning} "
            f"({self.field}, {direction})"
        )


def _entity_usages(platform: str, config: dict[str, Any]) -> Iterable[Usage]:
    writes = _WRITE_FIELDS.get(platform, frozenset())
    reads = _READ_FIELDS.get(platform, frozenset())
    entity_name = config.get("name", "<unnamed>")
    device_name = config.get("device_name")
    if device_name and device_name != entity_name:
        owner = (
            f"{platform} device {device_name!r}, entity {entity_name!r}"
        )
    else:
        owner = f"{platform} {entity_name!r}"
    for field in sorted(writes | reads):
        value = config.get(field)
        if value is None:
            continue
        is_write = field in writes
        if field in _MAP_FIELDS:
            if not isinstance(value, dict):
                continue
            for label, join in value.items():
                if isinstance(join, int):
                    detailed_field = f"{field}[{label}]"
                    yield Usage(
                        "d",
                        join,
                        owner,
                        detailed_field,
                        _field_meaning(platform, detailed_field),
                        is_write,
                    )
            continue
        if not isinstance(value, int):
            continue
        space = "a" if field in _ANALOG_FIELDS else "d"
        yield Usage(
            space,
            value,
            owner,
            field,
            _field_meaning(platform, field),
            is_write,
        )


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
        source = entry.get("entity_id")
        if source is None:
            source = "value_template" if "value_template" in entry else None
        owner = f"{label}[{index}]"
        if source:
            owner += f" {source!r}"
        yield Usage(
            space,
            int(number),
            owner,
            label,
            _field_meaning(label, label),
            writes,
        )


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


def build_join_metadata(yaml_config: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Build runtime log descriptions keyed by XSIG key (``a430``/``d5``).

    A join may legitimately have several readers plus one writer, so every
    claim is retained rather than choosing an arbitrary "owner".
    """
    usage = collect_join_usage(yaml_config)
    return {
        f"{space}{join}": tuple(claim.describe() for claim in claims)
        for (space, join), claims in usage.items()
    }
