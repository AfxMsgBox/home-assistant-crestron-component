"""The Crestron Integration Component"""

import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.helpers.event import TrackTemplate, async_track_template_result
from homeassistant.helpers.template import Template
from homeassistant.helpers.script import Script
from homeassistant.core import callback, Context
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
from .value_coercion import to_analog, to_digital, to_serial

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
    """Wrapper for the CrestronXsig library."""

    def __init__(self, hass, config):
        self.hass = hass
        self.hub = hass.data[DOMAIN][HUB] = CrestronXsig()
        self.port = config.get(CONF_PORT)
        self.context = Context()
        self.to_hub = {}
        self._template_to_join = {}  # id(template) -> join key
        self.from_hub = config.get(CONF_FROM_HUB, [])
        self._from_scripts = {}
        self.tracker = None

        self.hub.register_sync_all_joins_callback(self.sync_joins_to_hub)

        if CONF_TO_HUB in config:
            track_templates = []
            for entity in config[CONF_TO_HUB]:
                template = None
                if CONF_VALUE_TEMPLATE in entity:
                    template = entity[CONF_VALUE_TEMPLATE]
                elif CONF_ATTRIBUTE in entity and CONF_ENTITY_ID in entity:
                    template = Template(
                        "{{state_attr('"
                        + entity[CONF_ENTITY_ID]
                        + "','"
                        + entity[CONF_ATTRIBUTE]
                        + "')}}",
                        self.hass,
                    )
                elif CONF_ENTITY_ID in entity:
                    template = Template(
                        "{{states('" + entity[CONF_ENTITY_ID] + "')}}",
                        self.hass,
                    )
                if template is not None:
                    join = entity[CONF_JOIN]
                    self.to_hub[join] = template
                    self._template_to_join[id(template)] = join
                    track_templates.append(TrackTemplate(template, None))
            if track_templates:
                self.tracker = async_track_template_result(
                    self.hass, track_templates, self.template_change_callback
                )

        if self.from_hub:
            for entry in self.from_hub:
                self._from_scripts[entry[CONF_JOIN]] = Script(
                    self.hass,
                    entry[CONF_SCRIPT],
                    f"Crestron {entry[CONF_JOIN]}",
                    DOMAIN,
                )
            self.hub.register_callback(
                self.join_change_callback, joins=list(self._from_scripts.keys())
            )

    async def start(self):
        await self.hub.listen(self.port)

    async def stop(self):
        """Tear down: tracker, callbacks, server."""
        if self.from_hub:
            self.hub.remove_callback(self.join_change_callback)
        if self.tracker is not None:
            self.tracker.async_remove()
            self.tracker = None
        await self.hub.stop()

    async def join_change_callback(self, cbtype, value):
        """Run cached script for a configured from_joins entry."""
        script = self._from_scripts.get(cbtype)
        if script is None:
            return
        # For digital joins, only fire on rising edge (1) to avoid
        # double-trigger from momentary buttons.
        if cbtype[:1] == "d" and value == "0":
            return
        _LOGGER.debug(f"Running script for {cbtype} = {value}")
        # Run in background so a slow script can't block XSIG dispatch / TCP read.
        self.hass.async_create_task(self._run_script(script, cbtype, value))

    async def _run_script(self, script, cbtype, value):
        try:
            await script.async_run({"value": value}, self.context)
        except Exception:
            _LOGGER.exception("from_joins script for %s failed", cbtype)

    def _set_join(self, key, result):
        """Coerce template result and send to control system."""
        kind = key[:1]
        try:
            number = int(key[1:])
        except ValueError:
            _LOGGER.warning(f"Invalid join key: {key}")
            return
        if kind == "d":
            digital = to_digital(result)
            if digital is not None:
                self.hub.set_digital(number, digital)
        elif kind == "a":
            analog = to_analog(result)
            if analog is not None:
                self.hub.set_analog(number, analog)
        elif kind == "s":
            serial = to_serial(result)
            if serial is not None:
                self.hub.set_serial(number, serial)

    @callback
    def template_change_callback(self, event, updates):
        """Push template result to control system."""
        for track_template_result in updates:
            join = self._template_to_join.get(id(track_template_result.template))
            if join is not None:
                self._set_join(join, track_template_result.result)

    async def sync_joins_to_hub(self):
        _LOGGER.debug("Syncing joins to control system")
        for join, template in self.to_hub.items():
            # Isolate per-join failures: a single bad template (e.g. referencing
            # an unknown entity attribute) must not abort the rest of the sync
            # or bubble up and tear down the XSIG connection.
            try:
                self._set_join(join, template.async_render())
            except Exception:
                _LOGGER.exception("Failed to sync join %s to control system", join)
