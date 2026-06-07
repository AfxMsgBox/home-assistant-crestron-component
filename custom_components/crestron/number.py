"""Platform for Crestron Number (e.g. AC temperature setpoint) integration."""

import voluptuous as vol
import logging

from homeassistant.components.number import NumberEntity
from homeassistant.const import (
    CONF_NAME,
    CONF_DEVICE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

from .const import HUB, DOMAIN, YAML_CONF, CONF_VALUE_JOIN, CONF_MIN, CONF_MAX, CONF_STEP
from .schema import analog_join
from .device import device_info

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_VALUE_JOIN): analog_join,
        vol.Optional(CONF_MIN, default=16): vol.Coerce(float),
        vol.Optional(CONF_MAX, default=30): vol.Coerce(float),
        vol.Optional(CONF_STEP, default=1): vol.Coerce(float),
        vol.Optional(CONF_DEVICE_CLASS): cv.string,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("number", [])
    async_add_entities(CrestronNumber(hub, PLATFORM_SCHEMA(item)) for item in items)


class CrestronNumber(NumberEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._join = config.get(CONF_VALUE_JOIN)
        self._attr_native_min_value = config.get(CONF_MIN)
        self._attr_native_max_value = config.get(CONF_MAX)
        self._attr_native_step = config.get(CONF_STEP)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        self._attr_unique_id = f"crestron_number_{self._join}"
        self._attr_device_info = device_info(config)
        self._value = None  # optimistic/cached setpoint

    async def async_added_to_hass(self):
        self._hub.register_callback(self.process_callback, joins=[f"a{self._join}"])
        # If connected, trust live feedback; otherwise restore the pre-restart
        # value instead of showing 0/unknown until the control system next
        # pushes the analog join (it sends only on change).
        if self._hub.is_available():
            self._value = self._hub.get_analog(self._join)
        else:
            last = await self.async_get_last_state()
            if last is not None:
                try:
                    self._value = float(last.state)
                except (TypeError, ValueError):
                    pass

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self._value = self._hub.get_analog(self._join)
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def native_value(self):
        return self._value

    async def async_set_native_value(self, value):
        self._value = value
        self.async_write_ha_state()
        self._hub.set_analog(self._join, int(value))
