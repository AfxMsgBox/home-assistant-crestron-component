"""Platform for Crestron Sensor integration."""

import voluptuous as vol
import logging

from homeassistant.components.sensor import SensorEntity, CONF_STATE_CLASS
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS, CONF_UNIT_OF_MEASUREMENT
import homeassistant.helpers.config_validation as cv

from .const import HUB, DOMAIN, CONF_VALUE_JOIN, CONF_DIVISOR
from .schema import analog_join

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_VALUE_JOIN): analog_join,
        vol.Optional(CONF_DEVICE_CLASS): cv.string,
        vol.Optional(CONF_STATE_CLASS): cv.string,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
        vol.Optional(CONF_DIVISOR, default=1): vol.Coerce(float),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hub = hass.data[DOMAIN][HUB]
    async_add_entities([CrestronSensor(hub, config)])


class CrestronSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._join = config.get(CONF_VALUE_JOIN)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_state_class = config.get(CONF_STATE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        divisor = config.get(CONF_DIVISOR, 1)
        self._divisor = divisor if divisor else 1
        self._attr_unique_id = f"crestron_sensor_{self._join}"

    async def async_added_to_hass(self):
        self._hub.register_callback(
            self.process_callback, joins=[f"a{self._join}"]
        )

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def native_value(self):
        return self._hub.get_analog(self._join) / self._divisor
