"""Platform for Crestron Air Conditioner (climate) integration.

One climate entity per AC = one device, one thermostat card, shown in HA's
"空调"(climate) category. HA controls:
  - the running mode — a real, settable selector mapped to digital joins:
    cool/heat/dry/fan (制冷/制热/通风/除湿). Selecting a mode powers the unit on
    (pulse on_join if it was off) and asserts that mode's join, clearing the
    others (set-one-clear, like fan speed). HVACMode.OFF powers off.
  - power on/off — also available as the card's power button (TURN_ON/TURN_OFF).
  - temperature setpoint and fan speed.

Every join reports state back from the control system. Power state is read from
on_join's feedback level (on=1/off=0); the mode joins only say *which* mode is
running. fan/setpoint/temp are likewise reconciled from live feedback in
process_callback. An optimistic override is kept only for the brief window
between issuing a command and its feedback echo (so the UI doesn't bounce), and
state is restored across restarts via RestoreEntity.
"""

import asyncio
import logging
import time

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
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
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .join_commands import paired_feedback, pulse_digital, set_one_clear_others
from .unique_ids import climate_unique_id

_LOGGER = logging.getLogger(__name__)

# Room-temp feedback below this delta is dropped (not written to HA state),
# so a chatty control system can't flood the recorder database.
TEMP_REPORT_THRESHOLD = 0.5
# After issuing a power command, trust the optimistic power state for this long
# while the on/off feedback pair may still represent the previous state.
POWER_SETTLE_SECONDS = 2.0

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        # Power: momentary on/off command joins.
        vol.Required(CONF_ON_JOIN): digital_join,
        vol.Required(CONF_OFF_JOIN): digital_join,
        # Setpoint + current temperature (raw integer °C, no scaling).
        vol.Optional(CONF_SET_TEMP_JOIN): analog_join,
        vol.Optional(CONF_REG_TEMP_JOIN): analog_join,
        # Selectable running modes; CP4N returns state on the same joins.
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
    async_add_entities(
        setup_platform_entities(hass, "climate", PLATFORM_SCHEMA, CrestronAC)
    )


# Running-mode digital joins -> HVACMode, in a stable display order.
_MODE_ORDER = (
    (CONF_MODE_COOL_JOIN, HVACMode.COOL),
    (CONF_MODE_HEAT_JOIN, HVACMode.HEAT),
    (CONF_MODE_DRY_JOIN, HVACMode.DRY),
    (CONF_MODE_FAN_JOIN, HVACMode.FAN_ONLY),
)
class CrestronAC(CrestronEntity, ClimateEntity, RestoreEntity):
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
        self._attr_unique_id = climate_unique_id(config)
        self._attr_device_info = device_info(config)

        # Selectable modes: OFF + the real running modes. Fall back to a plain
        # on/off ([OFF, AUTO]) when no per-mode joins are configured.
        if self._mode_joins:
            self._attr_hvac_modes = [HVACMode.OFF] + list(self._mode_joins.keys())
        else:
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]

        # Optimistic/cached state (reconciled with feedback in process_callback).
        self._optimistic_on = False
        # Monotonic deadline until which the on/off feedback pair is ignored in
        # favour of optimistic state; 0 means no command is in flight.
        self._power_settle_until = 0.0
        self._optimistic_mode = next(iter(self._mode_joins), HVACMode.AUTO)
        self._target_temp = None
        self._fan_mode = None
        # Last room temp written to HA state; updates within
        # TEMP_REPORT_THRESHOLD of it are dropped.
        self._reported_temp = None
        self._reg_temp_cbtype = (
            f"a{self._reg_temp_join}" if self._reg_temp_join is not None else None
        )

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

    def _feedback_is_on(self):
        """Return definitive CP4N power feedback from the existing join pair."""
        return paired_feedback(self._hub, self._on_join, self._off_join)

    def _callback_joins(self):
        analog_joins = [
            j for j in (self._set_temp_join, self._reg_temp_join) if j is not None
        ]
        # The existing on/off command joins also carry CP4N's mutually-exclusive
        # power feedback. Subscribe to both; no extra state join is required.
        digital_joins = (
            [self._on_join, self._off_join]
            + list(self._mode_joins.values())
            + list(self._fan_joins.values())
        )
        joins = [f"a{j}" for j in analog_joins]
        joins += list(dict.fromkeys(f"d{j}" for j in digital_joins))
        return joins

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

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
            c = last.attributes.get("current_temperature")
            if c is not None:
                self._reported_temp = c
        # If already connected, upgrade to live feedback only when the on/off
        # pair is definitive; otherwise retain the restored cold-start state.
        if self._hub.is_available():
            power = self._feedback_is_on()
            if power is not None:
                self._optimistic_on = power
            fb = self._feedback_mode()
            if fb is not None:
                self._optimistic_mode = fb
            self._reconcile_temp_fan()

    def _reconcile_temp_fan(self):
        """Update cached setpoint/fan from live joins (ignore 0/None = unknown)."""
        if self._set_temp_join is not None:
            v = self._hub.get_analog(self._set_temp_join)
            if v:
                self._target_temp = v
        if self._reg_temp_join is not None:
            v = self._hub.get_analog(self._reg_temp_join)
            if v:
                self._reported_temp = v
        fb_fan = self._feedback_fan()
        if fb_fan is not None:
            self._fan_mode = fb_fan

    async def process_callback(self, cbtype, value):
        # Room-temp events: drop sub-threshold changes entirely so frequent
        # small fluctuations don't churn HA state / the recorder database.
        if cbtype == self._reg_temp_cbtype:
            v = self._hub.get_analog(self._reg_temp_join)
            if not v:  # 0/None = unknown, nothing to report
                return
            if (
                self._reported_temp is not None
                and abs(v - self._reported_temp) < TEMP_REPORT_THRESHOLD
            ):
                return
            self._reported_temp = v
            self._schedule_write()
            return
        # CP4N returns mutually-exclusive on/off levels on the existing command
        # joins. During the settle window right after a command, trust the
        # optimistic state because feedback may still represent the old state.
        if time.monotonic() >= self._power_settle_until:
            power = self._feedback_is_on()
            if power is not None:
                self._optimistic_on = power
        # Running-mode display follows whichever mode join is asserted.
        fb = self._feedback_mode()
        if fb is not None:
            self._optimistic_mode = fb
        self._reconcile_temp_fan()
        self._schedule_write()

    @property
    def hvac_mode(self):
        if not self._optimistic_on:
            return HVACMode.OFF
        if self._optimistic_mode in self._mode_joins:
            return self._optimistic_mode
        # Mode-less fallback (or mode not yet known): report the first non-off.
        return self._attr_hvac_modes[1]

    @property
    def current_temperature(self):
        return self._reported_temp

    @property
    def target_temperature(self):
        return self._target_temp

    @property
    def fan_mode(self):
        return self._fan_mode

    async def _pulse(self, join):
        await pulse_digital(self._hub, self._pulse_lock, join)

    async def async_turn_on(self):
        self._optimistic_on = True
        self._power_settle_until = time.monotonic() + POWER_SETTLE_SECONDS
        self.async_write_ha_state()
        await self._pulse(self._on_join)

    async def async_turn_off(self):
        self._optimistic_on = False
        self._power_settle_until = time.monotonic() + POWER_SETTLE_SECONDS
        self.async_write_ha_state()
        # Just pulse off_join; power state is read back from on_join. Leave the
        # mode joins as-is so the unit remembers its mode for the next power-on.
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
        self._power_settle_until = time.monotonic() + POWER_SETTLE_SECONDS
        self.async_write_ha_state()
        # Power on first if it was off, then select the mode (set-one-clear).
        if not was_on:
            await self._pulse(self._on_join)
        set_one_clear_others(self._hub, self._mode_joins.values(), join)

    async def async_set_fan_mode(self, fan_mode):
        target = self._fan_joins.get(fan_mode)
        if target is None:
            return
        self._fan_mode = fan_mode
        self.async_write_ha_state()
        set_one_clear_others(self._hub, self._fan_joins.values(), target)

    async def async_set_temperature(self, **kwargs):
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None and self._set_temp_join is not None:
            self._target_temp = temp
            self.async_write_ha_state()
            self._hub.set_analog(self._set_temp_join, int(temp))
