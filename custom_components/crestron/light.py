"""Platform for Crestron Light integration.

Two kinds of light share this platform:
  - dimmable: an analog brightness join (+ optional color-temperature join).
    开灯统一恢复模拟亮度；可选数字开关 Join 用于关灯和状态反馈。
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
    CONF_BRIGHTNESS_JOIN,
    CONF_COLOR_TEMP_JOIN,
    CONF_ON_JOIN,
    CONF_OFF_JOIN,
    CONF_SWITCH_JOIN,
    CONF_STATE_JOIN,
)
from .schema import analog_join, digital_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .join_commands import paired_feedback, pulse_digital
from .unique_ids import light_unique_id

_LOGGER = logging.getLogger(__name__)

# Hold the re-asserted level this long before sending 0 on turn_off, so the
# control system sees a distinct high->0 edge across program scans (a
# zero-delay re-assert+0 lands in one scan and the 0 gets missed).
OFF_REASSERT_SECONDS = 0.2
# 开灯前保持零电平，让只响应输入变化的主控识别随后恢复的亮度。
ON_RESET_SECONDS = 0.1
# 使用模拟量原值保存记忆，避免低亮度经过 0–255 换算后丢失精度。
ATTR_LAST_BRIGHTNESS = "last_nonzero_brightness"

def _require_light_capability(config):
    """Reject join combinations that cannot produce a controllable light.

    Every join is individually optional, so without this a hand-written entry
    with only a colour-temperature join, only half of an on/off pair, or no
    control join at all validated cleanly and then built an entity that can
    never be commanded (and, with no control join, a unique_id ending in
    ``None``). Mirrors the rules the xlsx converter applies to a row.
    """
    has_on = CONF_ON_JOIN in config
    has_off = CONF_OFF_JOIN in config
    if CONF_COLOR_TEMP_JOIN in config and CONF_BRIGHTNESS_JOIN not in config:
        raise vol.Invalid("color_temp_join requires brightness_join")
    if has_on != has_off:
        raise vol.Invalid(
            "on_join and off_join must be configured together (pulse mode)"
        )
    if not (CONF_BRIGHTNESS_JOIN in config or has_on or CONF_SWITCH_JOIN in config):
        raise vol.Invalid(
            "a light needs brightness_join, or both on_join and off_join, or "
            "switch_join; state_join alone is feedback with no way to control"
        )
    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_TYPE): cv.string,
            # Dimmable
            vol.Optional(CONF_BRIGHTNESS_JOIN): analog_join,
            vol.Optional(CONF_COLOR_TEMP_JOIN): analog_join,
            # 数字电源控制：调光灯也可配置，开/关必须成对。
            vol.Optional(CONF_ON_JOIN): digital_join,
            vol.Optional(CONF_OFF_JOIN): digital_join,
            vol.Optional(CONF_SWITCH_JOIN): digital_join,
            vol.Optional(CONF_STATE_JOIN): digital_join,
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_light_capability,
)


def _make_light(hub, cfg):
    # Capabilities come from the configured joins, not the descriptive `type`
    # field emitted by the xlsx converter. This keeps legacy hand-written YAML
    # working and prevents stale metadata from overriding real capabilities.
    if cfg.get(CONF_BRIGHTNESS_JOIN) is not None:
        return CrestronLight(hub, cfg)
    return CrestronOnOffLight(hub, cfg)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        setup_platform_entities(hass, "light", PLATFORM_SCHEMA, _make_light)
    )


class CrestronLight(CrestronEntity, LightEntity, RestoreEntity):
    # 双色温灯色温范围（开发者 xlsx「说明」：色温值 2700–6500），按原值 K 直写模拟 join。
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._brightness_join = config.get(CONF_BRIGHTNESS_JOIN)
        self._color_temp_join = config.get(CONF_COLOR_TEMP_JOIN)
        self._on_join = config.get(CONF_ON_JOIN)
        self._off_join = config.get(CONF_OFF_JOIN)
        self._switch_join = config.get(CONF_SWITCH_JOIN)
        self._state_join = config.get(CONF_STATE_JOIN)
        self._has_power_control = (
            self._on_join is not None or self._switch_join is not None
        )
        self._power_state = None
        self._power_off_requested = False
        self._power_command_lock = asyncio.Lock()
        self._pulse_lock = asyncio.Lock()
        self._attr_unique_id = light_unique_id(config)
        self._attr_device_info = device_info(config)
        self._command_seq = 0
        self._last_brightness = None
        if self._color_temp_join is not None:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS

    def _callback_joins(self):
        joins = [f"a{self._brightness_join}"]
        if self._color_temp_join is not None:
            joins.append(f"a{self._color_temp_join}")
        if self._has_power_control:
            if self._state_join is not None:
                joins.append(f"d{self._state_join}")
            elif self._switch_join is not None:
                joins.append(f"d{self._switch_join}")
            else:
                joins.extend([f"d{self._on_join}", f"d{self._off_join}"])
        return joins

    def _power_feedback(self):
        """数字反馈优先独立状态、保持开关、成对开关；不借用亮度推断。"""
        join = self._state_join if self._state_join is not None else self._switch_join
        if join is not None:
            if not self._hub.has_digital(join):
                return None
            return self._hub.get_digital(join)
        return paired_feedback(self._hub, self._on_join, self._off_join)

    def _remember_brightness(self, value):
        """只记住有效的非零亮度；关闭反馈不清除记忆。"""
        if type(value) is not int or not 1 <= value <= 65535:
            return False
        changed = self._last_brightness != value
        self._last_brightness = value
        return changed

    @property
    def extra_state_attributes(self):
        # 关灯时标准 brightness 属性不保留亮度，因此单独交给 RestoreEntity 保存。
        return {ATTR_LAST_BRIGHTNESS: self._last_brightness}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # 等待恢复期间收到的新反馈或新命令优先于历史值。
        if self._removed or self._last_brightness is not None:
            return
        if last is not None:
            self._remember_brightness(last.attributes.get(ATTR_LAST_BRIGHTNESS))
            # 兼容首次升级时只有标准亮度的开灯状态。
            if self._last_brightness is None and last.state == "on":
                previous = last.attributes.get("brightness")
                if type(previous) is int and 1 <= previous <= 255:
                    self._remember_brightness(round(previous / 255 * 65535))
        if self._hub.is_available() and self._hub.has_analog(self._brightness_join):
            self._remember_brightness(self._hub.get_analog(self._brightness_join))

    async def process_callback(self, cbtype, value):
        if self._removed:
            return
        # 仅亮度通道的新反馈更新记忆；色温和连接状态不能带回旧亮度缓存。
        if cbtype == f"a{self._brightness_join}":
            self._remember_brightness(self._hub.get_analog(self._brightness_join))
        if self._has_power_control and cbtype.startswith("d"):
            feedback = self._power_feedback()
            if feedback is not None:
                self._power_state = feedback
                if feedback is False:
                    self._power_off_requested = False
        self._schedule_write()

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
        if self._has_power_control:
            feedback = self._power_feedback()
            if feedback is not None:
                self._power_state = feedback
            # 两高/两低时保留最后明确反馈；首次无反馈为未知。
            return self._power_state
        return self._hub.get_analog(self._brightness_join) > 0

    def _set_color_temperature(self, kwargs):
        if self._color_temp_join is not None and "color_temp_kelvin" in kwargs:
            color_temp = int(max(
                self._attr_min_color_temp_kelvin,
                min(self._attr_max_color_temp_kelvin, kwargs["color_temp_kelvin"]),
            ))
            self._hub.set_analog(self._color_temp_join, color_temp)

    async def async_turn_on(self, **kwargs):
        if self._removed:
            return
        if self._has_power_control and kwargs.get("brightness") == 0:
            await self.async_turn_off()
            return
        self._command_seq += 1
        seq = self._command_seq
        if self._has_power_control:
            # 与数字关灯的完整脉冲串行；新请求先递增序号，淘汰旧等待任务。
            async with self._power_command_lock:
                if self._removed or seq != self._command_seq:
                    return
                await self._async_turn_on_brightness(seq, **kwargs)
        else:
            await self._async_turn_on_brightness(seq, **kwargs)

    async def _async_turn_on_brightness(self, seq, **kwargs):
        """关闭或未知时先置零，等待 0.1 秒后恢复亮度，不发送数字开灯。"""
        was_on = self.is_on
        if self._has_power_control:
            # 刚发过关灯、反馈仍旧为开时，也必须发送恢复亮度。
            was_on = was_on and not self._power_off_requested
            if not self._last_brightness and self._remember_brightness(
                self._hub.get_analog(self._brightness_join)
            ):
                self._schedule_write()
        level = None
        if "brightness" in kwargs:
            level = round(kwargs["brightness"] / 255 * 65535)
        elif not was_on:
            # 普通开灯恢复记忆；没有任何记录时才默认全亮。已开灯时不重设亮度。
            level = self._last_brightness or 65535

        if level is not None:
            # 先保存本次目标，使等待期间新的普通开灯仍能沿用该亮度。
            if self._remember_brightness(level):
                self._schedule_write()
            if level > 0 and not was_on:
                # 关灯反馈为零，但主控的 HA 输入可能仍保留上次非零命令。
                # 先置零并跨过一个扫描间隔，再恢复目标，避免同值被忽略。
                # 现场约束与不同方案的验证结果见 ARCH.md“调光灯修改前必读”。
                self._hub.set_analog(self._brightness_join, 0)
                await asyncio.sleep(ON_RESET_SECONDS)
                if self._removed or seq != self._command_seq:
                    return
            self._hub.set_analog(self._brightness_join, level)
            if level > 0:
                self._power_off_requested = False
            # 等待期间可能收到旧亮度反馈，实际发送后重新记住本次目标。
            if self._remember_brightness(level):
                self._schedule_write()

        self._set_color_temperature(kwargs)

    async def async_turn_off(self, **kwargs):
        if self._removed:
            return
        self._command_seq += 1
        seq = self._command_seq
        if self._has_power_control:
            async with self._power_command_lock:
                if self._removed or seq != self._command_seq:
                    return
                if not self._last_brightness and self._remember_brightness(
                    self._hub.get_analog(self._brightness_join)
                ):
                    self._schedule_write()
                self._power_off_requested = True
                if self._off_join is not None:
                    await pulse_digital(self._hub, self._pulse_lock, self._off_join)
                else:
                    self._hub.set_digital(self._switch_join, False)
            return
        # 快思聪调光模块通常只在 HA 下发的电平“发生变化”时才动作。物理开关开灯后，
        # HA 从未主动下发过电平，直接写 0 在控制系统看来像“没变化”而被忽略，灯泡
        # 关不掉。所以先把当前电平原样重发、保持一个程序扫描周期，再写 0，凑出一次
        # 明确的“高→0”跳变。两步之间必须有延时：零延时会让两个模拟量落进控制系统
        # 同一扫描周期、0 被重发值覆盖，导致第一次按关无效（要按两次）。
        current = self._hub.get_analog(self._brightness_join)
        # 仅在没有记忆时补录缓存，避免旧缓存覆盖刚发出但尚未反馈的调光值。
        if not self._last_brightness and self._remember_brightness(current):
            self._schedule_write()
        if current > 0:
            self._hub.set_analog(self._brightness_join, self._last_brightness or current)
            await asyncio.sleep(OFF_REASSERT_SECONDS)
            if self._removed or seq != self._command_seq:
                return
        self._hub.set_analog(self._brightness_join, 0)


class CrestronOnOffLight(CrestronEntity, LightEntity, RestoreEntity):
    """A light that only does on/off (relay-style), driven by digital joins.

    Mirrors the pulse + optimistic-state + restore behaviour of the switch
    platform, but presents as a light (ColorMode.ONOFF).
    """

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
        self._attr_unique_id = light_unique_id(config)

    def _callback_joins(self):
        if self._state_join is not None:
            return [f"d{self._state_join}"]
        if self._switch_join is not None:
            return [f"d{self._switch_join}"]
        # Pulse-only light: the control system reports state back on the command
        # joins themselves (on_join high = on, off_join high = off), so
        # subscribe to those to pick up panel/external changes.
        joins = []
        if self._on_join is not None:
            joins.append(f"d{self._on_join}")
        if self._off_join is not None:
            joins.append(f"d{self._off_join}")
        return joins

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
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

    def _feedback_is_on(self):
        if self._state_join is not None:
            # Unreported = unknown, so the restored/optimistic state stands.
            if not self._hub.has_digital(self._state_join):
                return None
            return self._hub.get_digital(self._state_join)
        if self._switch_join is not None:
            if not self._hub.has_digital(self._switch_join):
                return None
            return self._hub.get_digital(self._switch_join)
        # Pulse-only light: state comes back on the command joins themselves.
        return paired_feedback(self._hub, self._on_join, self._off_join)

    async def process_callback(self, cbtype, value):
        fb = self._feedback_is_on()
        if fb is not None:
            self._optimistic_state = fb
        self._schedule_write()

    @property
    def is_on(self):
        return self._optimistic_state

    async def _pulse(self, join):
        await pulse_digital(self._hub, self._pulse_lock, join)

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
