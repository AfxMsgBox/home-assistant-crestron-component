"""Platform for Crestron Binary Sensor integration."""

import voluptuous as vol
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS
import homeassistant.helpers.config_validation as cv

from .const import HUB, DOMAIN, YAML_CONF, CONF_IS_ON_JOIN
from .schema import digital_join

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_IS_ON_JOIN): digital_join,
        vol.Optional(CONF_DEVICE_CLASS): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("binary_sensor", [])
    async_add_entities(
        CrestronBinarySensor(hub, PLATFORM_SCHEMA(item)) for item in items
    )


class CrestronBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._join = config.get(CONF_IS_ON_JOIN)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_unique_id = f"crestron_binary_sensor_{self._join}"

    async def async_added_to_hass(self):
        self._hub.register_callback(
            self.process_callback, joins=[f"d{self._join}"]
        )

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def is_on(self):
        return self._hub.get_digital(self._join)
