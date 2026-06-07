"""Platform for Crestron Shades integration."""
import asyncio
import logging
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import CONF_NAME, CONF_TYPE
from .const import (
    HUB,
    DOMAIN,
    YAML_CONF,
    CONF_IS_OPENING_JOIN,
    CONF_IS_CLOSING_JOIN,
    CONF_IS_CLOSED_JOIN,
    CONF_OPEN_JOIN,
    CONF_CLOSE_JOIN,
    CONF_STOP_JOIN,
    CONF_POS_JOIN,
)
from .schema import analog_join, digital_join
from .device import device_info

_LOGGER = logging.getLogger(__name__)

PULSE_SECONDS = 0.2


def _require_drive_join(config):
    has_open = CONF_OPEN_JOIN in config
    has_close = CONF_CLOSE_JOIN in config
    if has_open != has_close:
        raise vol.Invalid(
            "open_join and close_join must be configured together"
        )
    if CONF_POS_JOIN in config or (has_open and has_close):
        return config
    raise vol.Invalid(
        "Either pos_join, or both open_join and close_join, must be configured"
    )


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Required(CONF_TYPE): cv.string,
            vol.Optional(CONF_POS_JOIN): analog_join,
            vol.Optional(CONF_OPEN_JOIN): digital_join,
            vol.Optional(CONF_CLOSE_JOIN): digital_join,
            vol.Optional(CONF_IS_OPENING_JOIN): digital_join,
            vol.Optional(CONF_IS_CLOSING_JOIN): digital_join,
            vol.Optional(CONF_IS_CLOSED_JOIN): digital_join,
            vol.Required(CONF_STOP_JOIN): digital_join,
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_drive_join,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("cover", [])
    async_add_entities(CrestronShade(hub, PLATFORM_SCHEMA(item)) for item in items)


class CrestronShade(CoverEntity):
    _attr_should_poll = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._is_opening_join = config.get(CONF_IS_OPENING_JOIN)
        self._is_closing_join = config.get(CONF_IS_CLOSING_JOIN)
        self._is_closed_join = config.get(CONF_IS_CLOSED_JOIN)
        self._open_join = config.get(CONF_OPEN_JOIN)
        self._close_join = config.get(CONF_CLOSE_JOIN)
        self._stop_join = config.get(CONF_STOP_JOIN)
        self._pos_join = config.get(CONF_POS_JOIN)
        self._pulse_lock = asyncio.Lock()
        self._attr_unique_id = (
            f"crestron_cover_{self._pos_join or self._open_join or self._close_join}"
        )
        self._attr_device_info = device_info(config)
        if config.get(CONF_TYPE) == "curtain":
            self._attr_device_class = CoverDeviceClass.CURTAIN
        else:
            self._attr_device_class = CoverDeviceClass.SHADE
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )
        if self._pos_join is not None:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION

    async def async_added_to_hass(self):
        joins = []
        if self._pos_join is not None:
            joins.append(f"a{self._pos_join}")
        for j in (
            self._is_opening_join,
            self._is_closing_join,
            self._is_closed_join,
        ):
            if j is not None:
                joins.append(f"d{j}")
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def current_cover_position(self):
        if self._pos_join is not None:
            return round(self._hub.get_analog(self._pos_join) / 65535 * 100)
        # Without a position feedback we can only report 0/100 from the
        # is_closed feedback. Anything else would be a fabricated value.
        if self._is_closed_join is not None:
            return 0 if self._hub.get_digital(self._is_closed_join) else 100
        return None

    @property
    def is_opening(self):
        if self._is_opening_join is None:
            return False
        return self._hub.get_digital(self._is_opening_join)

    @property
    def is_closing(self):
        if self._is_closing_join is None:
            return False
        return self._hub.get_digital(self._is_closing_join)

    @property
    def is_closed(self):
        if self._is_closed_join is not None:
            return self._hub.get_digital(self._is_closed_join)
        if self._pos_join is not None:
            return self._hub.get_analog(self._pos_join) == 0
        return None

    async def _pulse(self, join):
        async with self._pulse_lock:
            self._hub.set_digital(join, True)
            await asyncio.sleep(PULSE_SECONDS)
            self._hub.set_digital(join, False)

    async def async_set_cover_position(self, **kwargs):
        position = max(0, min(100, int(kwargs["position"])))
        if self._pos_join is not None:
            self._hub.set_analog(self._pos_join, round(position / 100 * 65535))
        elif self._open_join is not None and self._close_join is not None:
            if position > 50:
                await self.async_open_cover(**kwargs)
            else:
                await self.async_close_cover(**kwargs)

    async def async_open_cover(self, **kwargs):
        if self._open_join is not None:
            await self._pulse(self._open_join)
        elif self._pos_join is not None:
            self._hub.set_analog(self._pos_join, 65535)

    async def async_close_cover(self, **kwargs):
        if self._close_join is not None:
            await self._pulse(self._close_join)
        elif self._pos_join is not None:
            self._hub.set_analog(self._pos_join, 0)

    async def async_stop_cover(self, **kwargs):
        await self._pulse(self._stop_join)
        self.async_write_ha_state()
