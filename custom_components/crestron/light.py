"""Platform for Crestron Light integration."""
import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.const import CONF_NAME, CONF_TYPE
from .const import HUB, DOMAIN, CONF_BRIGHTNESS_JOIN, CONF_COLOR_TEMP_JOIN
from .schema import analog_join
from .device import device_info

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_TYPE): cv.string,
        vol.Required(CONF_BRIGHTNESS_JOIN): analog_join,
        vol.Optional(CONF_COLOR_TEMP_JOIN): analog_join,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hub = hass.data[DOMAIN][HUB]
    async_add_entities([CrestronLight(hub, config)])


class CrestronLight(LightEntity):
    _attr_should_poll = False
    _attr_min_color_temp_kelvin = 1500
    _attr_max_color_temp_kelvin = 5000

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._brightness_join = config.get(CONF_BRIGHTNESS_JOIN)
        self._color_temp_join = config.get(CONF_COLOR_TEMP_JOIN)
        self._attr_unique_id = f"crestron_light_{self._brightness_join}"
        self._attr_device_info = device_info(config)
        if self._color_temp_join is not None:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS

    async def async_added_to_hass(self):
        joins = [f"a{self._brightness_join}"]
        if self._color_temp_join is not None:
            joins.append(f"a{self._color_temp_join}")
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def brightness(self):
        analog = self._hub.get_analog(self._brightness_join)
        if analog <= 0:
            return 0
        # Never round a non-zero level down to 0: HA treats brightness 0 as off,
        # which would contradict is_on (analog > 0) for very dim levels.
        return max(1, round(analog / 65535 * 255))

    @property
    def color_temp_kelvin(self):
        if self._color_temp_join is None:
            return None
        value = self._hub.get_analog(self._color_temp_join)
        if value == 0:
            return self._attr_min_color_temp_kelvin
        return max(
            self._attr_min_color_temp_kelvin,
            min(self._attr_max_color_temp_kelvin, int(value)),
        )

    @property
    def is_on(self):
        return self._hub.get_analog(self._brightness_join) > 0

    async def async_turn_on(self, **kwargs):
        if "brightness" in kwargs:
            self._hub.set_analog(
                self._brightness_join, round(kwargs["brightness"] / 255 * 65535)
            )
        elif not self.is_on:
            # Only restore to full brightness when the light is currently off.
            # Avoids stomping on an already-set level when the user changes
            # only the color temperature.
            self._hub.set_analog(self._brightness_join, 65535)

        if self._color_temp_join is not None and "color_temp_kelvin" in kwargs:
            color_temp = int(
                max(
                    self._attr_min_color_temp_kelvin,
                    min(self._attr_max_color_temp_kelvin, kwargs["color_temp_kelvin"]),
                )
            )
            self._hub.set_analog(self._color_temp_join, color_temp)

    async def async_turn_off(self, **kwargs):
        # 快思聪调光模块通常只在 HA 下发的电平“发生变化”时才动作。
        # 物理开关开灯后，HA 从未主动下发过电平，直接写 0 在控制系统看来
        # 像“没变化”而被忽略，灯泡关不掉。所以先把当前电平原样重发一次，
        # 让紧接着的 0 成为一次明确的“高→0”跳变——等价于手动“先调亮度再关”。
        current = self._hub.get_analog(self._brightness_join)
        if current > 0:
            self._hub.set_analog(self._brightness_join, current)
        self._hub.set_analog(self._brightness_join, 0)
