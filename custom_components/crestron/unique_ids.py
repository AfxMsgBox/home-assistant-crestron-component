"""Final entity unique-ID rules.

Every platform builds its ID here so the rules stay visible in one place.
The integration deliberately does not rewrite Home Assistant's entity registry:
if an entity disappears from YAML or an ID rule changes during development, its
old registry entry remains unavailable until the user removes it.

Names and optional feedback joins are excluded from IDs. Relay-style entities
use a mandatory control join. Selects and mode sensors use their complete,
sorted join group so YAML ordering cannot change the ID and overlapping groups
remain distinct.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    CONF_BRIGHTNESS_JOIN,
    CONF_IS_ON_JOIN,
    CONF_MODE_JOINS,
    CONF_ON_JOIN,
    CONF_OPEN_JOIN,
    CONF_OPTIONS,
    CONF_POS_JOIN,
    CONF_SOURCE_NUM_JOIN,
    CONF_STATE_JOIN,
    CONF_SWITCH_JOIN,
    CONF_VALUE_JOIN,
)

_LOGGER = logging.getLogger(__name__)


def _digital(value: Any) -> str | None:
    return None if value is None else f"d{value}"


def light_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID for either a dimmable or relay-style light."""
    brightness = config.get(CONF_BRIGHTNESS_JOIN)
    if brightness is not None:
        return f"crestron_light_{brightness}"
    control = config.get(CONF_SWITCH_JOIN)
    if control is None:
        control = config.get(CONF_ON_JOIN)
    if control is None:
        # Defensive fallback for old hand-written feedback-only configurations.
        control = config.get(CONF_STATE_JOIN)
    return f"crestron_light_onoff_{_digital(control)}"


def switch_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on the writable control join."""
    control = config.get(CONF_SWITCH_JOIN)
    if control is None:
        control = config.get(CONF_ON_JOIN)
    return f"crestron_switch_{_digital(control)}"


def cover_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on open, with pos-only as a fallback."""
    control = config.get(CONF_OPEN_JOIN)
    if control is not None:
        return f"crestron_cover_{_digital(control)}"
    return f"crestron_cover_{config.get(CONF_POS_JOIN)}"


def climate_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on the mandatory power-on control join."""
    return f"crestron_climate_{_digital(config.get(CONF_ON_JOIN))}"


def _group_joins(joins: Any) -> list[int]:
    """Return sorted integer joins from a ``{label: join}`` mapping."""
    if not isinstance(joins, dict):
        return []
    return sorted(
        join
        for join in joins.values()
        if isinstance(join, int) and not isinstance(join, bool)
    )


def _group_key(joins: Any) -> str | None:
    """Return an order-independent identity for a complete join group."""
    values = _group_joins(joins)
    if not values:
        return None
    return "_".join(str(value) for value in values)


def select_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID for a select's option group."""
    return f"crestron_select_{_group_key(config.get(CONF_OPTIONS))}"


def sensor_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID for an analog or mode sensor."""
    mode_joins = config.get(CONF_MODE_JOINS)
    if mode_joins:
        return f"crestron_sensor_mode_{_group_key(mode_joins)}"
    return f"crestron_sensor_{config.get(CONF_VALUE_JOIN)}"


def binary_sensor_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on the sensor's only join."""
    return f"crestron_binary_sensor_{config.get(CONF_IS_ON_JOIN)}"


def number_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on the mandatory analog join."""
    return f"crestron_number_{config.get(CONF_VALUE_JOIN)}"


def media_player_unique_id(config: dict[str, Any]) -> str:
    """Return the final ID based on the source-select join."""
    return f"crestron_media_{config.get(CONF_SOURCE_NUM_JOIN)}"


_ID_BUILDERS = {
    "binary_sensor": binary_sensor_unique_id,
    "climate": climate_unique_id,
    "cover": cover_unique_id,
    "light": light_unique_id,
    "media_player": media_player_unique_id,
    "number": number_unique_id,
    "select": select_unique_id,
    "sensor": sensor_unique_id,
    "switch": switch_unique_id,
}


def _entities(yaml_config: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    """Return mapping entries for one platform, ignoring malformed rows."""
    entries = yaml_config.get(platform)
    if not isinstance(entries, list):
        return []
    valid = [entry for entry in entries if isinstance(entry, dict)]
    skipped = len(entries) - len(valid)
    if skipped:
        _LOGGER.warning(
            "Ignoring %d malformed %s entr%s while checking unique IDs "
            "(each must be a mapping)",
            skipped,
            platform,
            "y" if skipped == 1 else "ies",
        )
    return valid


def duplicate_unique_ids(yaml_config: dict[str, Any]) -> list[str]:
    """Report configured entities that would collide on unique_id."""
    seen: dict[tuple[str, str], list[str]] = {}
    for platform, build in _ID_BUILDERS.items():
        for config in _entities(yaml_config, platform):
            try:
                unique_id = build(config)
            except Exception:  # malformed entry; the platform will skip it
                continue
            name = str(config.get("name") or "<unnamed>")
            seen.setdefault((platform, unique_id), []).append(name)
    return [
        f"{platform} unique_id {unique_id!r} is shared by "
        f"{len(names)} entities: {', '.join(names)}"
        for (platform, unique_id), names in sorted(seen.items())
        if len(names) > 1
    ]
