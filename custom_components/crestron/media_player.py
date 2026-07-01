"""Platform for Crestron Media Player integration."""

import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerDeviceClass,
    MediaPlayerState,
)
from homeassistant.const import CONF_NAME
from .const import (
    CONF_MUTE_JOIN,
    CONF_VOLUME_JOIN,
    CONF_SOURCE_NUM_JOIN,
    CONF_SOURCES,
)
from .schema import analog_join, digital_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities

_LOGGER = logging.getLogger(__name__)

SOURCES_SCHEMA = vol.Schema(
    {
        cv.positive_int: cv.string,
    }
)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_MUTE_JOIN): digital_join,
        vol.Required(CONF_SOURCE_NUM_JOIN): analog_join,
        vol.Required(CONF_VOLUME_JOIN): analog_join,
        vol.Required(CONF_SOURCES): SOURCES_SCHEMA,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        setup_platform_entities(hass, "media_player", PLATFORM_SCHEMA, CrestronRoom)
    )


class CrestronRoom(CrestronEntity, MediaPlayerEntity):
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._mute_join = config.get(CONF_MUTE_JOIN)
        self._volume_join = config.get(CONF_VOLUME_JOIN)
        self._source_number_join = config.get(CONF_SOURCE_NUM_JOIN)
        self._sources = config.get(CONF_SOURCES)
        self._attr_unique_id = f"crestron_media_{self._source_number_join}"
        self._attr_device_info = device_info(config)
        self._last_source_num = next(iter(self._sources), None)

    def _callback_joins(self):
        return [
            f"d{self._mute_join}",
            f"a{self._volume_join}",
            f"a{self._source_number_join}",
        ]

    async def process_callback(self, cbtype, value):
        if cbtype == f"a{self._source_number_join}":
            current = self._hub.get_analog(self._source_number_join)
            if current and current in self._sources:
                self._last_source_num = current
        self.async_write_ha_state()

    @property
    def source_list(self):
        return list(self._sources.values())

    @property
    def source(self):
        source_num = self._hub.get_analog(self._source_number_join)
        if source_num == 0:
            return None
        return self._sources.get(source_num)

    @property
    def state(self):
        if self._hub.get_analog(self._source_number_join) == 0:
            return MediaPlayerState.OFF
        return MediaPlayerState.ON

    @property
    def is_volume_muted(self):
        return self._hub.get_digital(self._mute_join)

    @property
    def volume_level(self):
        return self._hub.get_analog(self._volume_join) / 65535

    async def async_mute_volume(self, mute):
        self._hub.set_digital(self._mute_join, mute)

    async def async_set_volume_level(self, volume):
        self._hub.set_analog(self._volume_join, int(volume * 65535))

    async def async_select_source(self, source):
        for input_num, name in self._sources.items():
            if name == source:
                self._hub.set_analog(self._source_number_join, input_num)
                return

    async def async_turn_on(self):
        if self._last_source_num is not None:
            self._hub.set_analog(self._source_number_join, self._last_source_num)

    async def async_turn_off(self):
        self._hub.set_analog(self._source_number_join, 0)
