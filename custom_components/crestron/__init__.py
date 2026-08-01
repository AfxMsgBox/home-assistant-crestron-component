"""The Crestron Integration Component"""

from functools import partial
import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_RELOAD,
    CONF_VALUE_TEMPLATE,
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
)
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service

from .crestron import CrestronXsig
from .const import (
    CONF_PORT, HUB, DOMAIN, CONF_JOIN, CONF_SCRIPT, CONF_TO_HUB, CONF_FROM_HUB,
    YAML_CONF, HUB_WRAPPER,
)
from .schema import join_key
from .bridge import ToJoinBridge, FromJoinBridge
from .join_registry import build_join_metadata, find_conflicts, usage_summary
from .unique_ids import duplicate_unique_ids

_LOGGER = logging.getLogger(__name__)


def _require_to_join_source(entry):
    """A to_join needs something to render, or it is silently inert.

    ``_build_template`` returns None when neither an entity nor a template is
    given (and ``attribute`` on its own has no entity to read from), so the
    join was accepted at load time and then simply never written.
    """
    if CONF_VALUE_TEMPLATE in entry or CONF_ENTITY_ID in entry:
        if CONF_ATTRIBUTE in entry and CONF_ENTITY_ID not in entry:
            raise vol.Invalid("attribute requires entity_id")
        return entry
    raise vol.Invalid(
        f"to_joins entry for {entry.get(CONF_JOIN)!r} needs entity_id or "
        f"value_template; otherwise nothing is ever sent to this join"
    )


TO_JOINS_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_JOIN): join_key,
            vol.Optional(CONF_ENTITY_ID): cv.entity_id,
            vol.Optional(CONF_ATTRIBUTE): cv.string,
            vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
        }
    ),
    _require_to_join_source,
)

FROM_JOINS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN): join_key,
        vol.Required(CONF_SCRIPT): cv.SCRIPT_SCHEMA,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_PORT): cv.port,
                vol.Optional(CONF_TO_HUB): vol.All(cv.ensure_list, [TO_JOINS_SCHEMA]),
                vol.Optional(CONF_FROM_HUB): vol.All(cv.ensure_list, [FROM_JOINS_SCHEMA]),
                # Entity definitions live under their platform key (light:, switch:,
                # number:, ...) and are validated per-entity by each platform.
            },
            extra=vol.ALLOW_EXTRA,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PLATFORMS = [
    "binary_sensor",
    "sensor",
    "switch",
    "light",
    "climate",
    "cover",
    "media_player",
    "number",
    "select",
]

# Everything that may legitimately appear directly under `crestron:`.
_KNOWN_CONFIG_KEYS = frozenset(PLATFORMS) | {CONF_PORT, CONF_TO_HUB, CONF_FROM_HUB}


def _warn_unknown_config_keys(domain_config):
    """Log and return typo'd keys directly under `crestron:`.

    CONFIG_SCHEMA has to allow extra keys, because entity definitions live
    under their platform key and are validated by each platform rather than
    here. The cost is that a mistyped platform key (`lights:` for `light:`) is
    accepted and then quietly ignored: no entities appear, nothing is logged,
    and there is nothing in the UI to explain why. Call this out explicitly.

    Startup remains permissive for compatibility, but reload uses the returned
    list to refuse a typo that would otherwise remove an entire platform.
    """
    unknown = sorted(
        (k for k in domain_config if k not in _KNOWN_CONFIG_KEYS), key=str
    )
    if not unknown:
        return []
    _LOGGER.warning(
        "Unrecognised key(s) under `crestron:`: %s — check for typos "
        "(entities go under a platform key, e.g. `light:` not `lights:`). "
        "Recognised keys: %s",
        ", ".join(map(str, unknown)),
        ", ".join(sorted(_KNOWN_CONFIG_KEYS)),
    )
    return unknown


def _warn_join_conflicts(yaml_conf):
    """Log joins claimed by two owners; see join_registry for the rules."""
    conflicts = find_conflicts(yaml_conf)
    if conflicts:
        _LOGGER.warning(
            "%d Crestron join conflict(s) found in configuration — two owners "
            "on one signal fight over it, and duplicate to_joins/from_joins "
            "keys silently lose all but the last entry:\n  %s",
            len(conflicts),
            "\n  ".join(conflicts),
        )
    # Separate check: colliding IDs would make Home Assistant drop entities
    # outright, and read-only join sharing (which is legitimate) can still
    # collide when two entities derive an ID from the same lowest join.
    duplicates = duplicate_unique_ids(yaml_conf)
    if duplicates:
        _LOGGER.warning(
            "%d duplicate Crestron unique ID(s); Home Assistant will drop all "
            "but the first entity of each set:\n  %s",
            len(duplicates),
            "\n  ".join(duplicates),
        )


async def async_setup(hass, config):
    """Stash the YAML config and create the config entry that platforms attach to.

    Entity definitions stay in YAML (under `crestron:`), but they are set up via
    a config entry so Home Assistant groups them into devices (device_info is
    only honoured for config-entry platforms, not legacy YAML platforms).
    """
    if config.get(DOMAIN) is None:
        return True

    _warn_unknown_config_keys(config[DOMAIN])
    _warn_join_conflicts(config[DOMAIN])
    hass.data.setdefault(DOMAIN, {})[YAML_CONF] = config[DOMAIN]
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data={}
        )
    )
    return True


def _platform_schemas():
    """Map platform key -> PLATFORM_SCHEMA.

    Imported lazily: at reload time these modules are already in
    ``sys.modules`` (the running entry forwarded to them), so this is a cache
    hit rather than disk I/O on the event loop.
    """
    from . import (
        binary_sensor, climate, cover, light, media_player, number, select,
        sensor, switch,
    )

    return {
        "binary_sensor": binary_sensor.PLATFORM_SCHEMA,
        "climate": climate.PLATFORM_SCHEMA,
        "cover": cover.PLATFORM_SCHEMA,
        "light": light.PLATFORM_SCHEMA,
        "media_player": media_player.PLATFORM_SCHEMA,
        "number": number.PLATFORM_SCHEMA,
        "select": select.PLATFORM_SCHEMA,
        "sensor": sensor.PLATFORM_SCHEMA,
        "switch": switch.PLATFORM_SCHEMA,
    }


def _invalid_entities(domain_config):
    """Validate the new config, split by how bad each problem is.

    Returns ``(row_problems, structural_problems)``, both
    ``[(platform, name, why)]``. The split decides whether a reload may go
    ahead:

    - A **row** problem is one bad entity. Startup skips those individually and
      keeps the rest, so reload must too — otherwise one typo makes the config
      unreloadable.
    - A **structural** problem is a whole section written as the wrong type
      (``light:`` as a mapping instead of a list). There is no way to read the
      user's intent, and going ahead would delete every light they have while
      reporting success. That has to stop the reload.
    """
    rows = []
    structural = []
    for platform, schema in _platform_schemas().items():
        entries = domain_config.get(platform)
        if entries is None:
            continue
        if not isinstance(entries, list):
            structural.append(
                (
                    platform,
                    "<whole section>",
                    f"must be a list of entities, got {type(entries).__name__}",
                )
            )
            continue
        for index, item in enumerate(entries):
            try:
                schema(item)
            except Exception as err:
                name = item.get("name") if isinstance(item, dict) else None
                rows.append(
                    (platform, name or f"#{index}", str(err).split("\n")[0])
                )
    return rows, structural


async def _reload_entry(hass, entry):
    """Reload one entry; True only if it is demonstrably loaded afterwards.

    Three independent things can each mean "this failed", and any one of them
    left unchecked lets a broken reload be reported as a success:
    ``async_reload`` returning False, it raising, or the entry simply not
    ending up LOADED. Treat the union as failure.
    """
    try:
        result = await hass.config_entries.async_reload(entry.entry_id)
    except Exception:
        _LOGGER.exception("Reloading config entry %s failed", entry.entry_id)
        return False
    if result is False:
        _LOGGER.error("Config entry %s did not reload", entry.entry_id)
        return False
    if entry.state is not ConfigEntryState.LOADED:
        _LOGGER.error(
            "Config entry %s is %s after reloading", entry.entry_id, entry.state
        )
        return False
    return True


async def _async_reload_yaml(hass, call):
    """Re-read `crestron:` from configuration.yaml and reload the entry.

    Entity definitions live in YAML but are set up through a config entry, and
    `async_setup` (the only thing that reads YAML) runs once at startup. Without
    this service, reloading the entry would just replay the stale copy in
    hass.data, so every edit — including a freshly generated crestron.yaml with
    hundreds of entities — needed a full Home Assistant restart.

    Failure has to leave a *working* integration behind, so the previous config
    is kept until the new one has actually loaded. The case that matters is a
    port that cannot be bound (already in use): the entry fails to set up, and
    without a rollback the operator is left with no entities at all and the old
    config already overwritten.
    """
    config = await async_integration_yaml_config(hass, DOMAIN)
    if config is None or DOMAIN not in config:
        _LOGGER.error(
            "Reload failed: no valid `crestron:` block in configuration.yaml. "
            "The previous configuration stays active."
        )
        return

    domain_config = config[DOMAIN]
    unknown = _warn_unknown_config_keys(domain_config)
    if unknown:
        _LOGGER.error(
            "Refusing to reload: unrecognised key(s) under `crestron:`: %s. "
            "A misspelled platform key would remove all entities from that "
            "platform. The previous configuration stays active.",
            ", ".join(map(str, unknown)),
        )
        return
    _warn_join_conflicts(domain_config)
    problems, structural = _invalid_entities(domain_config)
    if structural:
        _LOGGER.error(
            "Refusing to reload: %d section(s) under `crestron:` have the "
            "wrong shape, and loading them would delete every entity they "
            "should contain:\n  %s\nThe previous configuration stays active.",
            len(structural),
            "\n  ".join(f"{p}: {why}" for p, _n, why in structural),
        )
        return
    if problems:
        _LOGGER.warning(
            "%d entit%s in the new configuration will be skipped:\n  %s",
            len(problems),
            "y" if len(problems) == 1 else "ies",
            "\n  ".join(f"{p}: {n} — {why}" for p, n, why in problems),
        )

    domain_data = hass.data.setdefault(DOMAIN, {})
    previous = domain_data.get(YAML_CONF)
    domain_data[YAML_CONF] = domain_config

    entries = hass.config_entries.async_entries(DOMAIN)
    failed = []
    for entry in entries:
        if not await _reload_entry(hass, entry):
            failed.append(entry)

    if failed:
        if previous is None:
            _LOGGER.error(
                "Reload failed for %d config entr%s and there is no previous "
                "configuration to fall back to. Fix configuration.yaml and "
                "reload again.",
                len(failed),
                "y" if len(failed) == 1 else "ies",
            )
            return
        _LOGGER.error(
            "Reload failed for %d config entr%s (a port already in use is the "
            "usual cause); rolling back to the previous configuration.",
            len(failed),
            "y" if len(failed) == 1 else "ies",
        )
        domain_data[YAML_CONF] = previous
        still_broken = [
            entry.entry_id
            for entry in failed
            if not await _reload_entry(hass, entry)
        ]
        if still_broken:
            # The old config not coming back means something outside the config
            # is wrong (the port is held by another process entirely). Say so;
            # silently "rolling back" to a dead integration is worse than loud.
            _LOGGER.error(
                "Rollback did not restore %d config entr%s (%s). The "
                "integration is not running — check that the port is free, "
                "then reload again.",
                len(still_broken),
                "y" if len(still_broken) == 1 else "ies",
                ", ".join(still_broken),
            )
        else:
            _LOGGER.warning(
                "Rolled back to the previous Crestron configuration."
            )
        return

    _LOGGER.info(
        "Crestron configuration reloaded from configuration.yaml (%d entit%s "
        "skipped)",
        len(problems),
        "y" if len(problems) == 1 else "ies",
    )


async def async_setup_entry(hass, entry):
    """Start the hub and forward entity platforms (entities come from YAML)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    yaml_conf = domain_data.get(YAML_CONF)
    if yaml_conf is None:
        _LOGGER.error(
            "Crestron config entry loaded but no `crestron:` block found in "
            "configuration.yaml — add it (see README) and restart."
        )
        return False

    if not hass.services.has_service(DOMAIN, SERVICE_RELOAD):
        # Admin-only: reloading rebinds the TCP port and rebuilds every entity,
        # which is administration, not something a scripted user should trigger.
        async_register_admin_service(
            hass, DOMAIN, SERVICE_RELOAD, partial(_async_reload_yaml, hass)
        )

    hub = CrestronHub(hass, yaml_conf)
    await hub.start()
    domain_data[HUB_WRAPPER] = hub

    async def _stop(event):
        await hub.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # The server is already listening and the bridges are subscribed; a
        # failed setup must not leave the port bound, or the next reload gets
        # "address already in use" from our own orphan.
        domain_data.pop(HUB_WRAPPER, None)
        await hub.stop()
        raise
    return True


async def async_unload_entry(hass, entry):
    """Unload platforms and stop the hub."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hub = hass.data[DOMAIN].pop(HUB_WRAPPER, None)
        if hub is not None:
            await hub.stop()
    return unloaded


class CrestronHub:
    """Owns the XSIG instance + server lifecycle; composes the two bridges.

    Data-flow wiring lives in ``bridge.py`` (ToJoinBridge / FromJoinBridge);
    this class is just the Home Assistant lifecycle glue.
    """

    def __init__(self, hass, config):
        self.hass = hass
        self.config = config
        self.hub = hass.data[DOMAIN][HUB] = CrestronXsig(
            build_join_metadata(config)
        )
        self.port = config.get(CONF_PORT)

        self.to_bridge = ToJoinBridge(hass, self.hub, config.get(CONF_TO_HUB))
        self.from_bridge = FromJoinBridge(hass, self.hub, config.get(CONF_FROM_HUB))

        # The control system's sync-all request re-renders every to_join.
        self.hub.register_sync_all_joins_callback(self._sync_all)
        # A fresh connection needs more than that, so it gets its own hook.
        self.hub.register_connect_callback(self._on_connect)

    async def _sync_all(self):
        self.to_bridge.sync_all()

    async def _on_connect(self):
        """Runs once per accepted connection, before its first join frame.

        Two things have to happen at exactly this point:

        - ``from_joins`` edge detection must forget the previous session's
          levels, or the incoming full sync reads as a burst of button presses.
          This cannot ride on availability, which is deduplicated and emits
          nothing when a new connection takes over one that is still closing.
        - ``to_joins`` must be re-sent: the control system only knows the values
          it was last given, and 0xFD only asks it to report *its* joins, so
          panel feedback would otherwise sit stale until something changed.
        """
        self.from_bridge.reset_connection_baseline()
        self.to_bridge.sync_all()

    def resync_to_joins(self):
        """Manually re-render and resend every to_join to the control system.

        Exposed for the options flow so an operator can force HA's known state
        back onto the control system without waiting for a reconnect / 0xFB.
        """
        self.to_bridge.sync_all()

    def diagnostics(self):
        """Connection + cache snapshot, plus configured-entity counts.

        Combines the protocol layer's live view (connection, join caches) with
        the static YAML config (how many entities/to-joins/from-joins were
        configured) so a support download shows both what was set up and what
        the control system has actually reported.
        """
        configured = {
            platform: len(self.config.get(platform, []))
            for platform in PLATFORMS
            if self.config.get(platform)
        }
        return {
            "configured_entities": configured,
            "to_joins": len(self.config.get(CONF_TO_HUB, [])),
            "from_joins": len(self.config.get(CONF_FROM_HUB, [])),
            # Conflicts are logged once at setup; repeating them here means a
            # support download alone explains "two things fight over one join".
            "join_usage": usage_summary(self.config),
            "xsig": self.hub.diagnostics(),
        }

    async def start(self):
        """Bring up bridges + server, leaving nothing behind if it fails.

        ``listen()`` fails for a reason entirely outside this config — the port
        is already in use — and by then the template tracker is live. The
        caller has no handle to stop it (nothing has been stored in hass.data
        yet), so every failed setup or reload used to strand another tracker
        subscribed to entity state changes for the lifetime of the process.
        """
        self.to_bridge.start()
        self.from_bridge.start()
        try:
            await self.hub.listen(self.port)
        except Exception:
            self.from_bridge.stop()
            self.to_bridge.stop()
            raise

    async def stop(self):
        """Tear down: bridges first (remove callbacks/tracker), then server."""
        self.from_bridge.stop()
        self.to_bridge.stop()
        await self.hub.stop()
