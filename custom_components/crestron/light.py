"""Platform for Crestron Light integration.

Two kinds of light share this platform:
  - dimmable: an analog brightness join (+ optional color-temperature join).
  - on/off only: momentary on_join/off_join (relay-style). It is modelled as a
    light with ColorMode.ONOFF — i.e. a *light* with no brightness — rather
    than a switch, so a single-function ceiling light still behaves like a
    light (icon, "turn on the lights", the lights category) instead of a plug.
    The control system reports state back on the command joins themselves
    (on_join high = on, off_join high = off), so panel/external changes are
    reflected; an optional dedicated state_join/switch_join takes precedence.
"""
import asyncio
import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.const import CONF_NAME, CONF_TYPE
from homeassistant.helpers.restore_state import RestoreEntity
from .const import (
    HUB,
    DOMAIN,
    YAML_CONF,
    CONF_BRIGHTNESS_JOIN,
    CONF_COLOR_TEMP_JOIN,
    CONF_ON_JOIN,
    CONF_OFF_JOIN,
    CONF_SWITCH_JOIN,
    CONF_STATE_JOIN,
)
from .schema import analog_join, digital_join
from .device import device_info

_LOGGER = logging.getLogger(__name__)

PULSE_SECONDS = 0.2

# Hold the re-asserted level this long before sending 0 on turn_off, so the
# control system sees a distinct high->0 edge across program scans (a
# zero-delay re-assert+0 lands in one scan and the 0 gets missed).
OFF_REASSERT_SECONDS = 0.2

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_TYPE): cv.string,
        # Dimmable
        vol.Optional(CONF_BRIGHTNESS_JOIN): analog_join,
        vol.Optional(CONF_COLOR_TEMP_JOIN): analog_join,
        # On/off only (relay-style light)
        vol.Optional(CONF_ON_JOIN): digital_join,
        vol.Optional(CONF_OFF_JOIN): digital_join,
        vol.Optional(CONF_SWITCH_JOIN): digital_join,
        vol.Optional(CONF_STATE_JOIN): digital_join,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("light", [])
    entities = []
    for item in items:
        cfg = PLATFORM_SCHEMA(item)
        if cfg.get(CONF_BRIGHTNESS_JOIN) is not None:
            entities.append(CrestronLight(hub, cfg))
        else:
            entities.append(CrestronOnOffLight(hub, cfg))
    async_add_entities(entities)


class CrestronLight(LightEntity):
    _attr_should_poll = False
    # 双色温灯色温范围（开发者 xlsx「说明」：色温值 2700–6500），按原值 K 直写模拟 join。
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 6500

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
        # 快思聪调光模块通常只在 HA 下发的电平“发生变化”时才动作。物理开关开灯后，
        # HA 从未主动下发过电平，直接写 0 在控制系统看来像“没变化”而被忽略，灯泡
        # 关不掉。所以先把当前电平原样重发、保持一个程序扫描周期，再写 0，凑出一次
        # 明确的“高→0”跳变。两步之间必须有延时：零延时会让两个模拟量落进控制系统
        # 同一扫描周期、0 被重发值覆盖，导致第一次按关无效（要按两次）。
        current = self._hub.get_analog(self._brightness_join)
        if current > 0:
            self._hub.set_analog(self._brightness_join, current)
            await asyncio.sleep(OFF_REASSERT_SECONDS)
        self._hub.set_analog(self._brightness_join, 0)


class CrestronOnOffLight(LightEntity, RestoreEntity):
    """A light that only does on/off (relay-style), driven by digital joins.

    Mirrors the pulse + optimistic-state + restore behaviour of the switch
    platform, but presents as a light (ColorMode.ONOFF).
    """

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._on_join = config.get(CONF_ON_JOIN)
        self._off_join = config.get(CONF_OFF_JOIN)
        self._switch_join = config.get(CONF_SWITCH_JOIN)
        self._state_join = config.get(CONF_STATE_JOIN)
        self._attr_device_info = device_info(config)
        self._optimistic_state = False
        self._pulse_lock = asyncio.Lock()
        uid = self._switch_join or self._state_join or self._on_join
        self._attr_unique_id = f"crestron_light_onoff_{uid}_{self._attr_name}"

    async def async_added_to_hass(self):
        joins = []
        if self._state_join is not None:
            joins.append(f"d{self._state_join}")
        elif self._switch_join is not None:
            joins.append(f"d{self._switch_join}")
        else:
            # Pulse-only light: the control system reports state back on the
            # command joins themselves (on_join high = on, off_join high = off),
            # so subscribe to those to pick up panel/external changes.
            if self._on_join is not None:
                joins.append(f"d{self._on_join}")
            if self._off_join is not None:
                joins.append(f"d{self._off_join}")
        self._hub.register_callback(self.process_callback, joins=joins)
        # Restore the pre-restart state first (covers the cold-start window
        # before the control system reports the joins back).
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._optimistic_state = last.state == "on"
        # If already connected, upgrade to live feedback when it's definitive.
        if self._hub.is_available():
            fb = self._feedback_is_on()
            if fb is not None:
                self._optimistic_state = fb

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    def _feedback_is_on(self):
        if self._state_join is not None:
            return self._hub.get_digital(self._state_join)
        if self._switch_join is not None:
            return self._hub.get_digital(self._switch_join)
        # Pulse-only light: state comes back on the command joins themselves.
        # Exactly one asserted is definitive; both low (not reported yet) or
        # both high (a momentary transition) is indeterminate — keep current.
        on = self._on_join is not None and self._hub.get_digital(self._on_join)
        off = self._off_join is not None and self._hub.get_digital(self._off_join)
        if on != off:
            return on
        return None

    async def process_callback(self, cbtype, value):
        fb = self._feedback_is_on()
        if fb is not None:
            self._optimistic_state = fb
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def is_on(self):
        return self._optimistic_state

    async def _pulse(self, join):
        async with self._pulse_lock:
            self._hub.set_digital(join, True)
            await asyncio.sleep(PULSE_SECONDS)
            self._hub.set_digital(join, False)

    async def async_turn_on(self, **kwargs):
        self._optimistic_state = True
        self.async_write_ha_state()
        if self._on_join is not None:
            await self._pulse(self._on_join)
        elif self._switch_join is not None:
            self._hub.set_digital(self._switch_join, True)

    async def async_turn_off(self, **kwargs):
        self._optimistic_state = False
        self.async_write_ha_state()
        if self._off_join is not None:
            await self._pulse(self._off_join)
        elif self._switch_join is not None:
            self._hub.set_digital(self._switch_join, False)
