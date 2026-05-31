"""Platform for Crestron Thermostat integration."""

import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)

from homeassistant.const import CONF_NAME, ATTR_TEMPERATURE

FAN_ON = "on"
FAN_AUTO = "auto"

from .const import (
    HUB,
    DOMAIN,
    CONF_HEAT_SP_JOIN,
    CONF_COOL_SP_JOIN,
    CONF_REG_TEMP_JOIN,
    CONF_MODE_HEAT_JOIN,
    CONF_MODE_COOL_JOIN,
    CONF_MODE_AUTO_JOIN,
    CONF_MODE_OFF_JOIN,
    CONF_FAN_ON_JOIN,
    CONF_FAN_AUTO_JOIN,
    CONF_H1_JOIN,
    CONF_H2_JOIN,
    CONF_C1_JOIN,
    CONF_C2_JOIN,
    CONF_FA_JOIN,
)
from .schema import analog_join, digital_join

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_HEAT_SP_JOIN): analog_join,
        vol.Required(CONF_COOL_SP_JOIN): analog_join,
        vol.Required(CONF_REG_TEMP_JOIN): analog_join,
        vol.Required(CONF_MODE_HEAT_JOIN): digital_join,
        vol.Required(CONF_MODE_COOL_JOIN): digital_join,
        vol.Required(CONF_MODE_AUTO_JOIN): digital_join,
        vol.Required(CONF_MODE_OFF_JOIN): digital_join,
        vol.Required(CONF_FAN_ON_JOIN): digital_join,
        vol.Required(CONF_FAN_AUTO_JOIN): digital_join,
        vol.Required(CONF_H1_JOIN): digital_join,
        vol.Optional(CONF_H2_JOIN): digital_join,
        vol.Required(CONF_C1_JOIN): digital_join,
        vol.Optional(CONF_C2_JOIN): digital_join,
        vol.Required(CONF_FA_JOIN): digital_join,
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hub = hass.data[DOMAIN][HUB]
    entity = [CrestronThermostat(hub, config, hass.config.units.temperature_unit)]
    async_add_entities(entity)


class CrestronThermostat(ClimateEntity):
    _attr_should_poll = False
    _attr_hvac_modes = [
        HVACMode.HEAT_COOL,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.OFF,
    ]
    _attr_fan_modes = [FAN_ON, FAN_AUTO]
    _attr_supported_features = (
        ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    )

    def __init__(self, hub, config, unit):
        self._hub = hub
        self._attr_temperature_unit = unit
        self._attr_name = config[CONF_NAME]
        self._heat_sp_join = config[CONF_HEAT_SP_JOIN]
        self._cool_sp_join = config[CONF_COOL_SP_JOIN]
        self._reg_temp_join = config[CONF_REG_TEMP_JOIN]
        self._mode_heat_join = config[CONF_MODE_HEAT_JOIN]
        self._mode_cool_join = config[CONF_MODE_COOL_JOIN]
        self._mode_auto_join = config[CONF_MODE_AUTO_JOIN]
        self._mode_off_join = config[CONF_MODE_OFF_JOIN]
        self._fan_on_join = config[CONF_FAN_ON_JOIN]
        self._fan_auto_join = config[CONF_FAN_AUTO_JOIN]
        self._h1_join = config[CONF_H1_JOIN]
        self._h2_join = config.get(CONF_H2_JOIN)
        self._c1_join = config[CONF_C1_JOIN]
        self._c2_join = config.get(CONF_C2_JOIN)
        self._fa_join = config[CONF_FA_JOIN]
        self._attr_unique_id = f"crestron_climate_{self._reg_temp_join}"

    async def async_added_to_hass(self):
        analog_joins = (
            self._heat_sp_join,
            self._cool_sp_join,
            self._reg_temp_join,
        )
        digital_joins = (
            self._mode_heat_join,
            self._mode_cool_join,
            self._mode_auto_join,
            self._mode_off_join,
            self._fan_on_join,
            self._fan_auto_join,
            self._h1_join,
            self._h2_join,
            self._c1_join,
            self._c2_join,
            self._fa_join,
        )
        joins = [f"a{j}" for j in analog_joins if j is not None]
        joins += [f"d{j}" for j in digital_joins if j is not None]
        self._hub.register_callback(self.process_callback, joins=joins)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def current_temperature(self):
        return self._hub.get_analog(self._reg_temp_join) / 10

    @property
    def target_temperature_high(self):
        return self._hub.get_analog(self._cool_sp_join) / 10

    @property
    def target_temperature_low(self):
        return self._hub.get_analog(self._heat_sp_join) / 10

    @property
    def target_temperature(self):
        mode = self.hvac_mode
        if mode == HVACMode.HEAT:
            return self.target_temperature_low
        if mode == HVACMode.COOL:
            return self.target_temperature_high
        return None

    @property
    def hvac_mode(self):
        # mode_off wins: if the thermostat asserts off, treat it as off even
        # when stale heat/cool/auto bits haven't been cleared yet.
        if self._hub.get_digital(self._mode_off_join):
            return HVACMode.OFF
        if self._hub.get_digital(self._mode_auto_join):
            return HVACMode.HEAT_COOL
        if self._hub.get_digital(self._mode_heat_join):
            return HVACMode.HEAT
        if self._hub.get_digital(self._mode_cool_join):
            return HVACMode.COOL
        return HVACMode.OFF

    @property
    def fan_mode(self):
        if self._hub.get_digital(self._fan_on_join):
            return FAN_ON
        return FAN_AUTO

    @property
    def hvac_action(self):
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._h1_join and self._hub.get_digital(self._h1_join):
            return HVACAction.HEATING
        if self._h2_join and self._hub.get_digital(self._h2_join):
            return HVACAction.HEATING
        if self._c1_join and self._hub.get_digital(self._c1_join):
            return HVACAction.COOLING
        if self._c2_join and self._hub.get_digital(self._c2_join):
            return HVACAction.COOLING
        return HVACAction.IDLE

    async def async_set_hvac_mode(self, hvac_mode):
        target = {
            HVACMode.HEAT_COOL: self._mode_auto_join,
            HVACMode.HEAT: self._mode_heat_join,
            HVACMode.COOL: self._mode_cool_join,
            HVACMode.OFF: self._mode_off_join,
        }.get(hvac_mode)
        if target is None:
            return
        for join in (
            self._mode_auto_join,
            self._mode_heat_join,
            self._mode_cool_join,
            self._mode_off_join,
        ):
            if join != target:
                self._hub.set_digital(join, False)
        self._hub.set_digital(target, True)

    async def async_set_fan_mode(self, fan_mode):
        if fan_mode == FAN_AUTO:
            self._hub.set_digital(self._fan_on_join, False)
            self._hub.set_digital(self._fan_auto_join, True)
        elif fan_mode == FAN_ON:
            self._hub.set_digital(self._fan_auto_join, False)
            self._hub.set_digital(self._fan_on_join, True)

    async def async_set_temperature(self, **kwargs):
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        single = kwargs.get(ATTR_TEMPERATURE)
        if low is not None:
            self._hub.set_analog(self._heat_sp_join, int(float(low) * 10))
        if high is not None:
            self._hub.set_analog(self._cool_sp_join, int(float(high) * 10))
        if single is not None:
            mode = self.hvac_mode
            if mode == HVACMode.HEAT:
                self._hub.set_analog(self._heat_sp_join, int(float(single) * 10))
            elif mode == HVACMode.COOL:
                self._hub.set_analog(self._cool_sp_join, int(float(single) * 10))
