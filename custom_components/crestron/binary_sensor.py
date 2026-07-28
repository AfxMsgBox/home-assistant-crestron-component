"""Platform for Crestron Binary Sensor integration."""

import voluptuous as vol
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS
import homeassistant.helpers.config_validation as cv

from .const import CONF_IS_ON_JOIN
from .schema import digital_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .unique_ids import binary_sensor_unique_id

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
    async_add_entities(
        setup_platform_entities(
            hass, "binary_sensor", PLATFORM_SCHEMA, CrestronBinarySensor
        )
    )


class CrestronBinarySensor(CrestronEntity, BinarySensorEntity):
    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._join = config.get(CONF_IS_ON_JOIN)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_unique_id = binary_sensor_unique_id(config)
        self._attr_device_info = device_info(config)

    def _callback_joins(self):
        return [f"d{self._join}"]

    @property
    def is_on(self):
        # Unreported join = unknown, not off. The control system pushes on
        # change only, so before it has said anything about this join we have
        # no basis for asserting "off" (which automations would act on).
        if not self._hub.has_digital(self._join):
            return None
        return self._hub.get_digital(self._join)
