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
from .unique_ids import media_player_unique_id

_LOGGER = logging.getLogger(__name__)


def _source_number(value):
    """Strict input number: a whole number 1 or greater.

    Source 0 is this component's "off" value (``async_turn_off`` writes 0), so
    a source *numbered* 0 could be selected and then never turned back on.
    ``vol.Coerce(int)`` is too loose to enforce that on its own — it maps
    ``True`` to 1 and truncates ``1.9`` to 1, either of which silently
    collides with a real source. Plain decimal strings are accepted because
    YAML quoting is easy to do by accident.
    """
    if isinstance(value, bool):
        raise vol.Invalid(f"source number must be a whole number; got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or any(c < "0" or c > "9" for c in text):
            raise vol.Invalid(
                f"source number must be a whole number; got {value!r}"
            )
        value = int(text)
    if not isinstance(value, int):
        raise vol.Invalid(f"source number must be a whole number; got {value!r}")
    if value < 1:
        raise vol.Invalid(f"source numbers start at 1; 0 means off (got {value})")
    return value


def _sources(value):
    """Normalize and validate the whole source map without silent collisions."""
    if not isinstance(value, dict):
        raise vol.Invalid("sources must be a mapping of number to display name")
    if not value:
        raise vol.Invalid("sources must not be empty")

    normalized = {}
    names = set()
    for raw_number, raw_name in value.items():
        try:
            number = _source_number(raw_number)
            name = cv.string(raw_name)
        except vol.Invalid:
            raise
        except Exception as err:
            raise vol.Invalid(
                f"invalid source {raw_number!r}: {raw_name!r}"
            ) from err
        if number in normalized:
            raise vol.Invalid(
                f"duplicate source number {number} after normalization"
            )
        if name in names:
            raise vol.Invalid(f"duplicate source display name {name!r}")
        normalized[number] = name
        names.add(name)
    return normalized


# Non-empty: with no sources there is nothing to select and turn_on has no
# input number to restore.
SOURCES_SCHEMA = _sources

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
        self._attr_unique_id = media_player_unique_id(config)
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
        self._schedule_write()

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
        # Before the control system reports the source join we don't know
        # whether this zone is on; saying OFF would be a guess automations act
        # on. None renders as "unknown".
        if not self._hub.has_analog(self._source_number_join):
            return None
        if self._hub.get_analog(self._source_number_join) == 0:
            return MediaPlayerState.OFF
        return MediaPlayerState.ON

    @property
    def is_volume_muted(self):
        if not self._hub.has_digital(self._mute_join):
            return None
        return self._hub.get_digital(self._mute_join)

    @property
    def volume_level(self):
        if not self._hub.has_analog(self._volume_join):
            return None
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
