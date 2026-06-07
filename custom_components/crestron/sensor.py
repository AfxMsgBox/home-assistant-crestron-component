"""Platform for Crestron Sensor integration.

Two variants:
  - analog value sensor: ``value_join`` (number, optionally divided).
  - mode/text sensor: ``mode_joins`` ({label: digital_join}) reports the label
    of whichever join is high, or "关闭" when none are.
"""

import voluptuous as vol
import logging

from homeassistant.components.sensor import SensorEntity, CONF_STATE_CLASS
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS, CONF_UNIT_OF_MEASUREMENT
import homeassistant.helpers.config_validation as cv

from .const import HUB, DOMAIN, CONF_VALUE_JOIN, CONF_DIVISOR, CONF_MODE_JOINS
from .schema import analog_join, digital_join

_LOGGER = logging.getLogger(__name__)

MODE_OFF = "关闭"


def _require_one_source(config):
    has_value = CONF_VALUE_JOIN in config
    has_mode = CONF_MODE_JOINS in config
    if has_value == has_mode:
        raise vol.Invalid(
            "Configure exactly one of value_join or mode_joins"
        )
    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_VALUE_JOIN): analog_join,
            vol.Optional(CONF_MODE_JOINS): {cv.string: digital_join},
            vol.Optional(CONF_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_STATE_CLASS): cv.string,
            vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
            vol.Optional(CONF_DIVISOR, default=1): vol.Coerce(float),
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_one_source,
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
        self._mode_joins = config.get(CONF_MODE_JOINS) or {}
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_state_class = config.get(CONF_STATE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        divisor = config.get(CONF_DIVISOR, 1)
        self._divisor = divisor if divisor else 1
        if self._mode_joins:
            first = next(iter(self._mode_joins.values()))
            self._attr_unique_id = f"crestron_sensor_mode_{first}"
        else:
            self._attr_unique_id = f"crestron_sensor_{self._join}"

    async def async_added_to_hass(self):
        if self._mode_joins:
            joins = [f"d{j}" for j in self._mode_joins.values()]
        else:
            joins = [f"a{self._join}"]
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def native_value(self):
        if self._mode_joins:
            for label, join in self._mode_joins.items():
                if self._hub.get_digital(join):
                    return label
            return MODE_OFF
        return self._hub.get_analog(self._join) / self._divisor
