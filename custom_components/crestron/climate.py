"""Platform for Crestron Air Conditioner (climate) integration.

The AC is part of a larger centrally-controlled system: the running mode
(cool/heat/dry/fan) is **not** user-settable from HA — it is shown read-only via
``hvac_action`` (derived from the mode feedback joins). What HA controls is:
power on/off (momentary on/off joins), the temperature setpoint and the fan
speed.

Power state, setpoint and fan speed are kept optimistically and restored across
restarts: the control system pushes feedback only on change (no full dump on
connect), so reading the joins directly would show a wrong state until the next
change. process_callback reconciles to real feedback whenever it arrives.
"""

import asyncio
import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.components.climate.const import (
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_AUTO,
)
from homeassistant.const import CONF_NAME, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    HUB,
    DOMAIN,
    YAML_CONF,
    CONF_ON_JOIN,
    CONF_OFF_JOIN,
    CONF_SET_TEMP_JOIN,
    CONF_REG_TEMP_JOIN,
    CONF_MODE_COOL_JOIN,
    CONF_MODE_HEAT_JOIN,
    CONF_MODE_FAN_JOIN,
    CONF_MODE_DRY_JOIN,
    CONF_FAN_LOW_JOIN,
    CONF_FAN_MED_JOIN,
    CONF_FAN_HIGH_JOIN,
    CONF_FAN_AUTO_JOIN,
)
from .schema import analog_join, digital_join

_LOGGER = logging.getLogger(__name__)

PULSE_SECONDS = 0.2

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        # Power: momentary on/off command joins.
        vol.Required(CONF_ON_JOIN): digital_join,
        vol.Required(CONF_OFF_JOIN): digital_join,
        # Setpoint + current temperature (raw integer °C, no scaling).
        vol.Optional(CONF_SET_TEMP_JOIN): analog_join,
        vol.Optional(CONF_REG_TEMP_JOIN): analog_join,
        # Running-mode feedback (read-only): drives hvac_action display only.
        vol.Optional(CONF_MODE_COOL_JOIN): digital_join,
        vol.Optional(CONF_MODE_HEAT_JOIN): digital_join,
        vol.Optional(CONF_MODE_FAN_JOIN): digital_join,
        vol.Optional(CONF_MODE_DRY_JOIN): digital_join,
        # Fan speed (set-one-clear-others, state read from level).
        vol.Optional(CONF_FAN_LOW_JOIN): digital_join,
        vol.Optional(CONF_FAN_MED_JOIN): digital_join,
        vol.Optional(CONF_FAN_HIGH_JOIN): digital_join,
        vol.Optional(CONF_FAN_AUTO_JOIN): digital_join,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get("climate", [])
    async_add_entities(CrestronAC(hub, PLATFORM_SCHEMA(item)) for item in items)


class CrestronAC(ClimateEntity, RestoreEntity):
    _attr_should_poll = False
    # Mode is not user-settable, so we only expose off/on; the real running
    # mode is surfaced read-only through hvac_action.
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
    _attr_target_temperature_step = 1
    _attr_min_temp = 16
    _attr_max_temp = 30
    # AC reports raw integer Celsius; never tie this to the HA system unit or a
    # 25 gets reinterpreted as 25°F -> -3.9°C.
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # We implement async_turn_on/off ourselves; opt out of the legacy shim.
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config[CONF_NAME]
        self._on_join = config[CONF_ON_JOIN]
        self._off_join = config[CONF_OFF_JOIN]
        self._set_temp_join = config.get(CONF_SET_TEMP_JOIN)
        self._reg_temp_join = config.get(CONF_REG_TEMP_JOIN)
        self._mode_cool_join = config.get(CONF_MODE_COOL_JOIN)
        self._mode_heat_join = config.get(CONF_MODE_HEAT_JOIN)
        self._mode_fan_join = config.get(CONF_MODE_FAN_JOIN)
        self._mode_dry_join = config.get(CONF_MODE_DRY_JOIN)
        # fan mode string -> digital join
        self._fan_joins = {}
        if config.get(CONF_FAN_LOW_JOIN) is not None:
            self._fan_joins[FAN_LOW] = config[CONF_FAN_LOW_JOIN]
        if config.get(CONF_FAN_MED_JOIN) is not None:
            self._fan_joins[FAN_MEDIUM] = config[CONF_FAN_MED_JOIN]
        if config.get(CONF_FAN_HIGH_JOIN) is not None:
            self._fan_joins[FAN_HIGH] = config[CONF_FAN_HIGH_JOIN]
        if config.get(CONF_FAN_AUTO_JOIN) is not None:
            self._fan_joins[FAN_AUTO] = config[CONF_FAN_AUTO_JOIN]
        self._pulse_lock = asyncio.Lock()
        uid = self._reg_temp_join or self._set_temp_join or self._on_join
        self._attr_unique_id = f"crestron_climate_{uid}"

        # Optimistic/cached state (reconciled with feedback in process_callback).
        self._optimistic_on = False
        self._target_temp = None
        self._fan_mode = None

        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._set_temp_join is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._fan_joins:
            features |= ClimateEntityFeature.FAN_MODE
            self._attr_fan_modes = list(self._fan_joins.keys())
        self._attr_supported_features = features

    @property
    def _mode_joins(self):
        return [
            j
            for j in (
                self._mode_cool_join,
                self._mode_heat_join,
                self._mode_fan_join,
                self._mode_dry_join,
            )
            if j is not None
        ]

    def _feedback_fan(self):
        for mode, join in self._fan_joins.items():
            if self._hub.get_digital(join):
                return mode
        return None

    async def async_added_to_hass(self):
        analog_joins = [
            j for j in (self._set_temp_join, self._reg_temp_join) if j is not None
        ]
        digital_joins = self._mode_joins + list(self._fan_joins.values())
        joins = [f"a{j}" for j in analog_joins]
        joins += [f"d{j}" for j in digital_joins]
        self._hub.register_callback(self.process_callback, joins=joins)

        # Initial state: trust live feedback if connected, else restore the
        # pre-restart values (the control system pushes feedback only on change).
        last = await self.async_get_last_state()
        if last is not None:
            if last.state in (HVACMode.OFF, HVACMode.AUTO):
                self._optimistic_on = last.state == HVACMode.AUTO
            t = last.attributes.get(ATTR_TEMPERATURE)
            if t is not None:
                self._target_temp = t
            f = last.attributes.get("fan_mode")
            if f in self._fan_joins:
                self._fan_mode = f
        if self._hub.is_available():
            self._reconcile_from_feedback()

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    def _reconcile_from_feedback(self):
        """Update cached setpoint/fan from live joins (ignore 0/None = unknown)."""
        if self._set_temp_join is not None:
            v = self._hub.get_analog(self._set_temp_join)
            if v:
                self._target_temp = v
        fb_fan = self._feedback_fan()
        if fb_fan is not None:
            self._fan_mode = fb_fan

    async def process_callback(self, cbtype, value):
        self._reconcile_from_feedback()
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def hvac_mode(self):
        return HVACMode.AUTO if self._optimistic_on else HVACMode.OFF

    @property
    def hvac_action(self):
        # Read-only running mode from the central system's feedback joins.
        if not self._optimistic_on:
            return HVACAction.OFF
        if self._mode_cool_join and self._hub.get_digital(self._mode_cool_join):
            return HVACAction.COOLING
        if self._mode_heat_join and self._hub.get_digital(self._mode_heat_join):
            return HVACAction.HEATING
        if self._mode_dry_join and self._hub.get_digital(self._mode_dry_join):
            return HVACAction.DRYING
        if self._mode_fan_join and self._hub.get_digital(self._mode_fan_join):
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def current_temperature(self):
        if self._reg_temp_join is None:
            return None
        return self._hub.get_analog(self._reg_temp_join)

    @property
    def target_temperature(self):
        return self._target_temp

    @property
    def fan_mode(self):
        return self._fan_mode

    async def _pulse(self, join):
        async with self._pulse_lock:
            self._hub.set_digital(join, True)
            await asyncio.sleep(PULSE_SECONDS)
            self._hub.set_digital(join, False)

    async def async_turn_on(self):
        self._optimistic_on = True
        self.async_write_ha_state()
        await self._pulse(self._on_join)

    async def async_turn_off(self):
        self._optimistic_on = False
        self.async_write_ha_state()
        await self._pulse(self._off_join)

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    async def async_set_fan_mode(self, fan_mode):
        target = self._fan_joins.get(fan_mode)
        if target is None:
            return
        self._fan_mode = fan_mode
        self.async_write_ha_state()
        for join in self._fan_joins.values():
            if join != target:
                self._hub.set_digital(join, False)
        self._hub.set_digital(target, True)

    async def async_set_temperature(self, **kwargs):
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None and self._set_temp_join is not None:
            self._target_temp = temp
            self.async_write_ha_state()
            self._hub.set_analog(self._set_temp_join, int(temp))
