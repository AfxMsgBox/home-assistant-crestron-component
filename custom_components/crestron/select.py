"""Platform for Crestron Select (e.g. AC fan speed) integration.

Maps a set of named options to digital joins. Exactly one join is asserted
at a time (set the chosen one, clear the others); the current option is read
back from whichever join is high.
"""

import voluptuous as vol
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

from .const import HUB, DOMAIN, YAML_CONF, CONF_OPTIONS
from .schema import digital_join
from .device import device_info

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_OPTIONS): {cv.string: digital_join},
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("select", [])
    async_add_entities(CrestronSelect(hub, PLATFORM_SCHEMA(item)) for item in items)


class CrestronSelect(SelectEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._joins = dict(config.get(CONF_OPTIONS))  # label -> digital join
        self._attr_options = list(self._joins.keys())
        first_join = next(iter(self._joins.values()))
        self._attr_unique_id = f"crestron_select_{first_join}"
        self._attr_device_info = device_info(config)
        self._current = None  # optimistic/cached option

    async def async_added_to_hass(self):
        joins = [f"d{j}" for j in self._joins.values()]
        self._hub.register_callback(self.process_callback, joins=joins)
        # If connected, trust live feedback; otherwise restore the pre-restart
        # option instead of showing nothing until the control system next
        # pushes a join (it sends only on change).
        if self._hub.is_available():
            fb = self._feedback_option()
            if fb is not None:
                self._current = fb
        else:
            last = await self.async_get_last_state()
            if last is not None and last.state in self._attr_options:
                self._current = last.state

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    def _feedback_option(self):
        for label, join in self._joins.items():
            if self._hub.get_digital(join):
                return label
        return None

    async def process_callback(self, cbtype, value):
        # Reconcile with feedback; keep the last option during the brief window
        # where no join is high (mid-transition) so the value doesn't flicker.
        fb = self._feedback_option()
        if fb is not None:
            self._current = fb
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def current_option(self):
        return self._current

    async def async_select_option(self, option):
        target = self._joins.get(option)
        if target is None:
            return
        self._current = option
        self.async_write_ha_state()
        for join in self._joins.values():
            if join != target:
                self._hub.set_digital(join, False)
        self._hub.set_digital(target, True)
