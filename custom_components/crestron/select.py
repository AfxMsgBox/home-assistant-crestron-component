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

from .const import HUB, DOMAIN, CONF_OPTIONS
from .schema import digital_join

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_OPTIONS): {cv.string: digital_join},
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hub = hass.data[DOMAIN][HUB]
    async_add_entities([CrestronSelect(hub, config)])


class CrestronSelect(SelectEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._joins = dict(config.get(CONF_OPTIONS))  # label -> digital join
        self._attr_options = list(self._joins.keys())
        first_join = next(iter(self._joins.values()))
        self._attr_unique_id = f"crestron_select_{first_join}"

    async def async_added_to_hass(self):
        joins = [f"d{j}" for j in self._joins.values()]
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def current_option(self):
        for label, join in self._joins.items():
            if self._hub.get_digital(join):
                return label
        return None

    async def async_select_option(self, option):
        target = self._joins.get(option)
        if target is None:
            return
        for join in self._joins.values():
            if join != target:
                self._hub.set_digital(join, False)
        self._hub.set_digital(target, True)
