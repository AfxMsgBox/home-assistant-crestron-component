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

from .const import CONF_VALUE_JOIN, CONF_MIN, CONF_MAX, CONF_STEP
from .schema import analog_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .unique_ids import number_unique_id

_LOGGER = logging.getLogger(__name__)

def _require_usable_range(config):
    """min < max and step > 0; otherwise the slider is unusable or divides by 0."""
    if config[CONF_MIN] >= config[CONF_MAX]:
        raise vol.Invalid(
            f"min ({config[CONF_MIN]}) must be less than max ({config[CONF_MAX]})"
        )
    if config[CONF_STEP] <= 0:
        raise vol.Invalid(f"step must be greater than 0; got {config[CONF_STEP]}")
    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
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
    ),
    _require_usable_range,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        setup_platform_entities(hass, "number", PLATFORM_SCHEMA, CrestronNumber)
    )


class CrestronNumber(CrestronEntity, NumberEntity, RestoreEntity):
    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._join = config.get(CONF_VALUE_JOIN)
        self._attr_native_min_value = config.get(CONF_MIN)
        self._attr_native_max_value = config.get(CONF_MAX)
        self._attr_native_step = config.get(CONF_STEP)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        self._attr_unique_id = number_unique_id(config)
        self._attr_device_info = device_info(config)
        self._value = None  # optimistic/cached setpoint

    def _callback_joins(self):
        return [f"a{self._join}"]

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # If connected, trust live feedback; otherwise restore the pre-restart
        # value instead of showing 0/unknown until the control system next
        # pushes the analog join (it sends only on change). Treat 0 as "not yet
        # known" so the setpoint never briefly shows below its min (e.g. 16).
        if self._hub.is_available():
            v = self._hub.get_analog(self._join)
            if v:
                self._value = v
        else:
            last = await self.async_get_last_state()
            if last is not None:
                try:
                    self._value = float(last.state)
                except (TypeError, ValueError):
                    pass

    async def process_callback(self, cbtype, value):
        v = self._hub.get_analog(self._join)
        if v:
            self._value = v
        self._schedule_write()

    @property
    def native_value(self):
        return self._value

    async def async_set_native_value(self, value):
        self._value = value
        self.async_write_ha_state()
        self._hub.set_analog(self._join, int(value))
