"""The Crestron Integration Component"""

import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    CONF_VALUE_TEMPLATE,
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
)

from .crestron import CrestronXsig
from .const import (
    CONF_PORT, HUB, DOMAIN, CONF_JOIN, CONF_SCRIPT, CONF_TO_HUB, CONF_FROM_HUB,
    YAML_CONF, HUB_WRAPPER,
)
from .schema import join_key
from .bridge import ToJoinBridge, FromJoinBridge

_LOGGER = logging.getLogger(__name__)

TO_JOINS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN): join_key,
        vol.Optional(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_ATTRIBUTE): cv.string,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    }
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

async def async_setup(hass, config):
    """Stash the YAML config and create the config entry that platforms attach to.

    Entity definitions stay in YAML (under `crestron:`), but they are set up via
    a config entry so Home Assistant groups them into devices (device_info is
    only honoured for config-entry platforms, not legacy YAML platforms).
    """
    if config.get(DOMAIN) is None:
        return True

    hass.data.setdefault(DOMAIN, {})[YAML_CONF] = config[DOMAIN]
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data={}
        )
    )
    return True


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

    hub = CrestronHub(hass, yaml_conf)
    await hub.start()
    domain_data[HUB_WRAPPER] = hub

    async def _stop(event):
        await hub.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
        self.hub = hass.data[DOMAIN][HUB] = CrestronXsig()
        self.port = config.get(CONF_PORT)

        self.to_bridge = ToJoinBridge(hass, self.hub, config.get(CONF_TO_HUB))
        self.from_bridge = FromJoinBridge(hass, self.hub, config.get(CONF_FROM_HUB))

        # The control system's sync-all request re-renders every to_join.
        self.hub.register_sync_all_joins_callback(self._sync_all)

    async def _sync_all(self):
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
            "xsig": self.hub.diagnostics(),
        }

    async def start(self):
        self.to_bridge.start()
        self.from_bridge.start()
        await self.hub.listen(self.port)

    async def stop(self):
        """Tear down: bridges first (remove callbacks/tracker), then server."""
        self.from_bridge.stop()
        self.to_bridge.stop()
        await self.hub.stop()
