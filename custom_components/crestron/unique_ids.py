"""Stable entity unique IDs and migrations from historical formats.

Every platform's ID is built here so the rules stay visible in one place.

Entity identity must come from a control join that is present for the lifetime
of the device.  Names and optional feedback/capability joins are deliberately
excluded: renaming a device or adding position/temperature feedback must not
create a second Home Assistant entity.

For the same reason an ID derived from a *group* of joins (select options,
sensor mode joins) uses the lowest join number rather than whichever one YAML
happens to list first — reordering the options in the config file is an
editing convenience, not a new device.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Iterable

from .const import (
    CONF_BRIGHTNESS_JOIN,
    CONF_CLOSE_JOIN,
    CONF_DEVICE_ID,
    CONF_IS_ON_JOIN,
    CONF_MODE_JOINS,
    CONF_ON_JOIN,
    CONF_OPEN_JOIN,
    CONF_OPTIONS,
    CONF_POS_JOIN,
    CONF_REG_TEMP_JOIN,
    CONF_SET_TEMP_JOIN,
    CONF_SOURCE_NUM_JOIN,
    CONF_STATE_JOIN,
    CONF_SWITCH_JOIN,
    CONF_VALUE_JOIN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _digital(value: Any) -> str | None:
    return None if value is None else f"d{value}"


def light_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID for either a dimmable or relay-style light."""
    brightness = config.get(CONF_BRIGHTNESS_JOIN)
    if brightness is not None:
        # Preserve the historical dimmable-light format; brightness is already
        # the mandatory, stable control join.
        return f"crestron_light_{brightness}"
    control = config.get(CONF_SWITCH_JOIN)
    if control is None:
        control = config.get(CONF_ON_JOIN)
    if control is None:
        # Defensive fallback for legacy hand-written, feedback-only configs.
        control = config.get(CONF_STATE_JOIN)
    return f"crestron_light_onoff_{_digital(control)}"


def switch_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on the switch's writable control join."""
    control = config.get(CONF_SWITCH_JOIN)
    if control is None:
        control = config.get(CONF_ON_JOIN)
    return f"crestron_switch_{_digital(control)}"


def cover_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on open, with pos-only as a legacy fallback."""
    control = config.get(CONF_OPEN_JOIN)
    if control is not None:
        return f"crestron_cover_{_digital(control)}"
    return f"crestron_cover_{config.get(CONF_POS_JOIN)}"


def climate_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on the mandatory power-on control join."""
    return f"crestron_climate_{_digital(config.get(CONF_ON_JOIN))}"


def _lowest_join(joins: Any) -> Any:
    """Lowest join in a ``{label: join}`` map, order-independent.

    Tolerates junk: this runs *before* the platforms validate their configs, so
    it can be handed a list, a string, or a map mixing ints and strings. Only
    real ints are considered; anything else yields None and the caller falls
    back rather than raising and taking the whole integration down with it.
    """
    if not isinstance(joins, dict):
        return None
    values = [
        j
        for j in joins.values()
        if isinstance(j, int) and not isinstance(j, bool)
    ]
    return min(values) if values else None


def select_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID for a select's option group."""
    return f"crestron_select_{_lowest_join(config.get(CONF_OPTIONS))}"


def sensor_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID for either sensor variant.

    The schema requires exactly one of ``value_join`` / ``mode_joins``, and the
    two use different prefixes, so an analog sensor and a mode sensor can never
    collide even if the numbers coincide across signal spaces.
    """
    mode_joins = config.get(CONF_MODE_JOINS)
    if mode_joins:
        return f"crestron_sensor_mode_{_lowest_join(mode_joins)}"
    return f"crestron_sensor_{config.get(CONF_VALUE_JOIN)}"


def binary_sensor_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on the sensor's only join."""
    return f"crestron_binary_sensor_{config.get(CONF_IS_ON_JOIN)}"


def number_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on the number's mandatory analog join."""
    return f"crestron_number_{config.get(CONF_VALUE_JOIN)}"


def media_player_unique_id(config: dict[str, Any]) -> str:
    """Return a stable ID based on the source-select join."""
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


def duplicate_unique_ids(yaml_config: dict[str, Any]) -> list[str]:
    """Report configured entities that would collide on unique_id.

    Home Assistant silently drops whichever entity registers second, logging
    only a generic "does not generate unique IDs" warning that says nothing
    about which config lines are at fault. Group-derived IDs make this
    reachable in ordinary use: two read-only mode sensors mirroring overlapping
    join sets share their lowest join, and the join-conflict check deliberately
    stays quiet about read/read sharing, so nothing else would catch it.
    """
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


@dataclass(frozen=True)
class UniqueIdMigration:
    """One historical unique ID that should become a stable ID."""

    entity_domain: str
    new_unique_id: str
    old_unique_id: str
    old_prefix: str | None
    device_identifier: str | None
    name: str


def _legacy_join_uid(
    *, analog: Iterable[Any] = (), digital: Iterable[Any] = ()
) -> str | None:
    """Reproduce the pre-migration mixed-space ID selection."""
    for join in analog:
        if join is not None:
            return str(join)
    for join in digital:
        if join is not None:
            return f"d{join}"
    return None


def _entities(yaml_config: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    """Configured entries for one platform, skipping anything malformed.

    Migration planning runs before the platforms validate their own configs, so
    a hand-written entry that is a bare string (or anything else non-mapping)
    reaches this code. It must be skipped here: raising would abort
    ``async_setup_entry`` and take down every entity in the integration, when
    the designed behaviour is to drop the one bad entry and carry on.
    """
    entries = yaml_config.get(platform)
    if not isinstance(entries, list):
        return []
    valid = [entry for entry in entries if isinstance(entry, dict)]
    skipped = len(entries) - len(valid)
    if skipped:
        _LOGGER.warning(
            "Ignoring %d malformed %s entr%s while planning unique ID "
            "migrations (each must be a mapping)",
            skipped,
            platform,
            "y" if skipped == 1 else "ies",
        )
    return valid


def unique_id_migrations(
    yaml_config: dict[str, Any],
) -> list[UniqueIdMigration]:
    """Build migrations for the currently configured entities.

    The exact current-name legacy ID is preferred.  ``old_prefix`` lets the
    runtime also preserve a single entity after a YAML rename; if several old
    entries share that prefix, migration is intentionally left unresolved
    rather than guessing or deleting registry data.
    """
    migrations: list[UniqueIdMigration] = []

    for config in _entities(yaml_config, "light"):
        if config.get(CONF_BRIGHTNESS_JOIN) is not None:
            continue
        old_control = (
            config.get(CONF_SWITCH_JOIN)
            or config.get(CONF_STATE_JOIN)
            or config.get(CONF_ON_JOIN)
        )
        if old_control is None:
            continue
        old_base = f"crestron_light_onoff_{old_control}"
        old = f"{old_base}_{config.get('name')}"
        new = light_unique_id(config)
        if old != new:
            migrations.append(
                UniqueIdMigration(
                    "light",
                    new,
                    old,
                    f"{old_base}_",
                    config.get(CONF_DEVICE_ID),
                    str(config.get("name") or ""),
                )
            )

    for config in _entities(yaml_config, "switch"):
        old_control = (
            config.get(CONF_SWITCH_JOIN)
            or config.get(CONF_STATE_JOIN)
            or config.get(CONF_ON_JOIN)
        )
        if old_control is None:
            continue
        old_base = f"crestron_switch_{old_control}"
        old = f"{old_base}_{config.get('name')}"
        new = switch_unique_id(config)
        if old != new:
            migrations.append(
                UniqueIdMigration(
                    "switch",
                    new,
                    old,
                    f"{old_base}_",
                    config.get(CONF_DEVICE_ID),
                    str(config.get("name") or ""),
                )
            )

    for config in _entities(yaml_config, "cover"):
        old_part = _legacy_join_uid(
            analog=(config.get(CONF_POS_JOIN),),
            digital=(config.get(CONF_OPEN_JOIN), config.get(CONF_CLOSE_JOIN)),
        )
        if old_part is None:
            continue
        old = f"crestron_cover_{old_part}"
        new = cover_unique_id(config)
        migrations.append(
            UniqueIdMigration(
                "cover",
                new,
                old,
                None,
                config.get(CONF_DEVICE_ID),
                str(config.get("name") or ""),
            )
        )
        # Before digital/analog join namespaces were introduced, a digital-only
        # cover used the bare open-join number.
        if config.get(CONF_POS_JOIN) is None and config.get(CONF_OPEN_JOIN) is not None:
            bare_old = f"crestron_cover_{config[CONF_OPEN_JOIN]}"
            if bare_old != old:
                migrations.append(
                    UniqueIdMigration(
                        "cover",
                        new,
                        bare_old,
                        None,
                        config.get(CONF_DEVICE_ID),
                        str(config.get("name") or ""),
                    )
                )

    for config in _entities(yaml_config, "climate"):
        old_part = _legacy_join_uid(
            analog=(
                config.get(CONF_REG_TEMP_JOIN),
                config.get(CONF_SET_TEMP_JOIN),
            ),
            digital=(config.get(CONF_ON_JOIN),),
        )
        if old_part is None:
            continue
        old = f"crestron_climate_{old_part}"
        new = climate_unique_id(config)
        migrations.append(
            UniqueIdMigration(
                "climate",
                new,
                old,
                None,
                config.get(CONF_DEVICE_ID),
                str(config.get("name") or ""),
            )
        )
        # The oldest format used a bare on-join when no temperature join was
        # configured; retain that upgrade path too.
        if (
            config.get(CONF_REG_TEMP_JOIN) is None
            and config.get(CONF_SET_TEMP_JOIN) is None
        ):
            bare_old = f"crestron_climate_{config.get(CONF_ON_JOIN)}"
            if bare_old != old:
                migrations.append(
                    UniqueIdMigration(
                        "climate",
                        new,
                        bare_old,
                        None,
                        config.get(CONF_DEVICE_ID),
                        str(config.get("name") or ""),
                    )
                )

    # select / mode-sensor IDs used to be built from whichever join YAML listed
    # first, so reordering the options silently orphaned the entity. They are
    # keyed on the lowest join now; migrate anyone still on the old form.
    for platform, key, prefix in (
        ("select", CONF_OPTIONS, "crestron_select_"),
        ("sensor", CONF_MODE_JOINS, "crestron_sensor_mode_"),
    ):
        for config in _entities(yaml_config, platform):
            joins = config.get(key)
            if not isinstance(joins, dict) or not joins:
                continue
            lowest = _lowest_join(joins)
            if lowest is None:
                continue
            # The old ID came from whichever join YAML listed first *at the
            # time the entity was registered* — which we cannot read off the
            # current file, because the user may well have reordered the
            # options in the same edit that prompted the upgrade. So offer a
            # migration from every join in the group; async_migrate_unique_ids
            # only acts on one that actually exists in the registry, and stops
            # as soon as the new ID is present.
            candidates = sorted(
                j
                for j in joins.values()
                if isinstance(j, int) and not isinstance(j, bool)
            )
            for candidate in candidates:
                if candidate == lowest:
                    continue
                migrations.append(
                    UniqueIdMigration(
                        platform,
                        f"{prefix}{lowest}",
                        f"{prefix}{candidate}",
                        None,
                        config.get(CONF_DEVICE_ID),
                        str(config.get("name") or ""),
                    )
                )

    return migrations


async def async_migrate_unique_ids(
    hass: Any, yaml_config: dict[str, Any]
) -> None:
    """Migrate registry entries before entity platforms are forwarded."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    migrated = 0

    # Planning is defensive per entry, but it must never be the reason the
    # integration fails to start: preserving entity history is a nice-to-have,
    # having working entities is not.
    try:
        planned = unique_id_migrations(yaml_config)
    except Exception:
        _LOGGER.exception(
            "Could not plan unique ID migrations; continuing without them. "
            "Entities keep their current IDs."
        )
        return

    for migration in planned:
        if registry.async_get_entity_id(
            migration.entity_domain, DOMAIN, migration.new_unique_id
        ):
            continue

        entity_id = registry.async_get_entity_id(
            migration.entity_domain, DOMAIN, migration.old_unique_id
        )
        if entity_id is None and migration.old_prefix is not None:
            candidates = [
                entry.entity_id
                for entry in registry.entities.values()
                if entry.entity_id.startswith(f"{migration.entity_domain}.")
                and entry.platform == DOMAIN
                and entry.unique_id.startswith(migration.old_prefix)
            ]
            if len(candidates) == 1:
                entity_id = candidates[0]
            elif len(candidates) > 1:
                _LOGGER.warning(
                    "Cannot safely migrate %s (%s): %d legacy entities match "
                    "unique-ID prefix %s; leaving them unchanged",
                    migration.name,
                    migration.entity_domain,
                    len(candidates),
                    migration.old_prefix,
                )

        if entity_id is None and migration.device_identifier:
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, migration.device_identifier)}
            )
            if device is not None:
                candidates = [
                    entry.entity_id
                    for entry in registry.entities.values()
                    if entry.entity_id.startswith(f"{migration.entity_domain}.")
                    and entry.platform == DOMAIN
                    and entry.device_id == device.id
                ]
                if len(candidates) == 1:
                    entity_id = candidates[0]
                elif len(candidates) > 1:
                    _LOGGER.warning(
                        "Cannot safely migrate %s (%s): device %s has %d "
                        "candidate entities; leaving them unchanged",
                        migration.name,
                        migration.entity_domain,
                        migration.device_identifier,
                        len(candidates),
                    )

        if entity_id is None:
            continue

        registry.async_update_entity(
            entity_id, new_unique_id=migration.new_unique_id
        )
        migrated += 1
        _LOGGER.info(
            "Migrated %s unique ID for %s: %s -> %s",
            migration.entity_domain,
            entity_id,
            migration.old_unique_id,
            migration.new_unique_id,
        )

    if migrated:
        _LOGGER.info("Migrated %d Crestron entity unique ID(s)", migrated)
