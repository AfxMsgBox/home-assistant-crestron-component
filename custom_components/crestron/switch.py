"""Platform for Crestron Switch integration."""

import asyncio
import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS
from .const import (
    HUB,
    DOMAIN,
    CONF_SWITCH_JOIN,
    CONF_ON_JOIN,
    CONF_OFF_JOIN,
    CONF_STATE_JOIN,
)
from .schema import digital_join

_LOGGER = logging.getLogger(__name__)

PULSE_SECONDS = 0.2


def _require_writable_join(config):
    has_on = CONF_ON_JOIN in config
    has_off = CONF_OFF_JOIN in config
    has_direct = CONF_SWITCH_JOIN in config

    if has_on != has_off:
        raise vol.Invalid(
            "on_join and off_join must be configured together (pulse mode)"
        )
    has_pulse = has_on and has_off
    if has_pulse and has_direct:
        raise vol.Invalid(
            "switch_join cannot be combined with on_join/off_join; "
            "use state_join for feedback in pulse mode"
        )
    if not (has_pulse or has_direct):
        raise vol.Invalid(
            "Either switch_join, or both on_join and off_join, must be configured"
        )
    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_SWITCH_JOIN): digital_join,
            vol.Optional(CONF_ON_JOIN): digital_join,
            vol.Optional(CONF_OFF_JOIN): digital_join,
            vol.Optional(CONF_STATE_JOIN): digital_join,
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_writable_join,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hub = hass.data[DOMAIN][HUB]
    async_add_entities([CrestronSwitch(hub, config)])


class CrestronSwitch(SwitchEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._switch_join = config.get(CONF_SWITCH_JOIN)
        self._on_join = config.get(CONF_ON_JOIN)
        self._off_join = config.get(CONF_OFF_JOIN)
        self._state_join = config.get(CONF_STATE_JOIN)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._optimistic_state = False
        self._pulse_lock = asyncio.Lock()
        uid = self._switch_join or self._state_join or self._on_join
        self._attr_unique_id = f"crestron_switch_{uid}_{self._attr_name}"

    async def async_added_to_hass(self):
        joins = []
        if self._state_join is not None:
            joins.append(f"d{self._state_join}")
        elif self._switch_join is not None:
            joins.append(f"d{self._switch_join}")
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def _has_feedback_join(self):
        return self._state_join is not None or self._switch_join is not None

    @property
    def is_on(self):
        if self._state_join is not None:
            return self._hub.get_digital(self._state_join)
        if self._switch_join is not None:
            return self._hub.get_digital(self._switch_join)
        return self._optimistic_state

    async def _pulse(self, join):
        async with self._pulse_lock:
            self._hub.set_digital(join, True)
            await asyncio.sleep(PULSE_SECONDS)
            self._hub.set_digital(join, False)

    async def async_turn_on(self, **kwargs):
        if self._on_join is not None:
            await self._pulse(self._on_join)
            if not self._has_feedback_join:
                self._optimistic_state = True
                self.async_write_ha_state()
        else:
            self._hub.set_digital(self._switch_join, True)

    async def async_turn_off(self, **kwargs):
        if self._off_join is not None:
            await self._pulse(self._off_join)
            if not self._has_feedback_join:
                self._optimistic_state = False
                self.async_write_ha_state()
        else:
            self._hub.set_digital(self._switch_join, False)
