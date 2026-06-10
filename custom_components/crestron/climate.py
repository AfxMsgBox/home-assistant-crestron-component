"""Platform for Crestron Air Conditioner (climate) integration.

One climate entity per AC = one device, one thermostat card, shown in HA's
"空调"(climate) category. HA controls:
  - the running mode — a real, settable selector mapped to digital joins:
    cool/heat/dry/fan (制冷/制热/通风/除湿). Selecting a mode powers the unit on
    (pulse on_join if it was off) and asserts that mode's join, clearing the
    others (set-one-clear, like fan speed). HVACMode.OFF powers off.
  - power on/off — also available as the card's power button (TURN_ON/TURN_OFF).
  - temperature setpoint and fan speed.

Every join now reports state back from the control system, so power/mode/fan/
setpoint are reconciled from live feedback in process_callback. An optimistic
override is kept only for the brief window between issuing a command and its
feedback echo (so the UI doesn't bounce), and state is restored across restarts
via RestoreEntity.
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


# Running-mode digital joins -> HVACMode, in a stable display order.
_MODE_ORDER = (
    (CONF_MODE_COOL_JOIN, HVACMode.COOL),
    (CONF_MODE_HEAT_JOIN, HVACMode.HEAT),
    (CONF_MODE_DRY_JOIN, HVACMode.DRY),
    (CONF_MODE_FAN_JOIN, HVACMode.FAN_ONLY),
)
_MODE_TO_ACTION = {
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.HEAT: HVACAction.HEATING,
    HVACMode.DRY: HVACAction.DRYING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
}


class CrestronAC(ClimateEntity, RestoreEntity):
    _attr_should_poll = False
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
        # HVACMode -> digital join (only for the modes that carry a join).
        self._mode_joins = {}
        for conf_key, mode in _MODE_ORDER:
            join = config.get(conf_key)
            if join is not None:
                self._mode_joins[mode] = join
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

        # Selectable modes: OFF + the real running modes. Fall back to a plain
        # on/off ([OFF, AUTO]) when no per-mode joins are configured.
        if self._mode_joins:
            self._attr_hvac_modes = [HVACMode.OFF] + list(self._mode_joins.keys())
        else:
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        # Callback keys for the mode joins (used to detect external power-off).
        self._mode_cbtypes = {f"d{j}" for j in self._mode_joins.values()}

        # Optimistic/cached state (reconciled with feedback in process_callback).
        self._optimistic_on = False
        self._optimistic_mode = next(iter(self._mode_joins), HVACMode.AUTO)
        self._target_temp = None
        self._fan_mode = None

        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._set_temp_join is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._fan_joins:
            features |= ClimateEntityFeature.FAN_MODE
            self._attr_fan_modes = list(self._fan_joins.keys())
        self._attr_supported_features = features

    def _feedback_mode(self):
        """The HVACMode whose join is asserted, or None when all are low (off)."""
        for mode, join in self._mode_joins.items():
            if self._hub.get_digital(join):
                return mode
        return None

    def _feedback_fan(self):
        for mode, join in self._fan_joins.items():
            if self._hub.get_digital(join):
                return mode
        return None

    async def async_added_to_hass(self):
        analog_joins = [
            j for j in (self._set_temp_join, self._reg_temp_join) if j is not None
        ]
        digital_joins = list(self._mode_joins.values()) + list(self._fan_joins.values())
        joins = [f"a{j}" for j in analog_joins]
        joins += [f"d{j}" for j in digital_joins]
        self._hub.register_callback(self.process_callback, joins=joins)

        # Restore the pre-restart state first (covers the cold-start window
        # before the control system reconnects).
        last = await self.async_get_last_state()
        if last is not None:
            if last.state == HVACMode.OFF:
                self._optimistic_on = False
            elif last.state in self._mode_joins:
                self._optimistic_on = True
                self._optimistic_mode = HVACMode(last.state)
            elif last.state == HVACMode.AUTO and not self._mode_joins:
                self._optimistic_on = True
            t = last.attributes.get(ATTR_TEMPERATURE)
            if t is not None:
                self._target_temp = t
            f = last.attributes.get("fan_mode")
            if f in self._fan_joins:
                self._fan_mode = f
        # If already connected, upgrade to live feedback. Only *raise* to a known
        # mode here — don't force "off" from not-yet-pushed joins on cold start.
        if self._hub.is_available():
            fb = self._feedback_mode()
            if fb is not None:
                self._optimistic_on = True
                self._optimistic_mode = fb
            self._reconcile_temp_fan()

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    def _reconcile_temp_fan(self):
        """Update cached setpoint/fan from live joins (ignore 0/None = unknown)."""
        if self._set_temp_join is not None:
            v = self._hub.get_analog(self._set_temp_join)
            if v:
                self._target_temp = v
        fb_fan = self._feedback_fan()
        if fb_fan is not None:
            self._fan_mode = fb_fan

    async def process_callback(self, cbtype, value):
        # Reconcile power/mode from feedback. A mode join going high means on +
        # that mode; a mode-join event with all modes low means the unit was
        # switched off (incl. outside HA). A non-mode event (temp/fan/available)
        # never flips power, so the optimistic on-state set by a command isn't
        # clobbered before its feedback echoes.
        fb = self._feedback_mode()
        if fb is not None:
            self._optimistic_on = True
            self._optimistic_mode = fb
        elif cbtype in self._mode_cbtypes:
            self._optimistic_on = False
        self._reconcile_temp_fan()
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def hvac_mode(self):
        if not self._optimistic_on:
            return HVACMode.OFF
        if self._optimistic_mode in self._mode_joins:
            return self._optimistic_mode
        # Mode-less fallback (or mode not yet known): report the first non-off.
        return self._attr_hvac_modes[1]

    @property
    def hvac_action(self):
        if not self._optimistic_on:
            return HVACAction.OFF
        return _MODE_TO_ACTION.get(self._optimistic_mode, HVACAction.IDLE)

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
            return
        join = self._mode_joins.get(hvac_mode)
        if join is None:
            # Mode-less fallback (e.g. AUTO): just power on.
            await self.async_turn_on()
            return
        was_on = self._optimistic_on
        self._optimistic_on = True
        self._optimistic_mode = hvac_mode
        self.async_write_ha_state()
        # Power on first if it was off, then select the mode (set-one-clear).
        if not was_on:
            await self._pulse(self._on_join)
        for j in self._mode_joins.values():
            if j != join:
                self._hub.set_digital(j, False)
        self._hub.set_digital(join, True)

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
