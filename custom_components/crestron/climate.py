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
the on_join/off_join feedback pair; the mode joins only say *which* mode is
running. fan/setpoint/temp are likewise reconciled from live feedback in
process_callback. Each field consumes its own new feedback instead of rereading
unrelated cached joins. Commands on one entity run in order, including the
power pulse before a mode selection. State is restored across restarts.
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
from .schema import analog_join, digital_join, whole_number
from .crestron import AVAILABLE_KEY
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .join_commands import pulse_digital, set_one_clear_others
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
        self._command_lock = asyncio.Lock()
        # Only received levels belong here; a command invalidates its group's
        # old levels without altering the shared hub's real feedback cache.
        self._feedback_levels = {}
        self._power_settle_handle = None
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
        return self._group_feedback(self._mode_joins)

    def _feedback_fan(self):
        return self._group_feedback(self._fan_joins)

    def _group_feedback(self, joins):
        """Accept one asserted option; all-low/multiple-high is ambiguous."""
        active = [
            option for option, join in joins.items()
            if self._feedback_levels.get(join)
        ]
        return active[0] if len(active) == 1 else None

    def _forget_feedback(self, joins):
        for join in joins:
            self._feedback_levels.pop(join, None)

    def _feedback_is_on(self):
        return self._group_feedback({True: self._on_join, False: self._off_join})

    def _accept_feedback(self, attribute, value, label):
        old = getattr(self, attribute)
        if old != value:
            _LOGGER.debug(
                "%s: %s feedback %r -> %r", self._attr_name, label, old, value
            )
            setattr(self, attribute, value)

    def _reconcile_power(self):
        if time.monotonic() < self._power_settle_until:
            return
        power = self._feedback_is_on()
        if power is not None:
            self._accept_feedback("_optimistic_on", power, "power")

    def _cancel_power_settle(self):
        if self._power_settle_handle is not None:
            self._power_settle_handle.cancel()
            self._power_settle_handle = None

    def _begin_power_command(self, on):
        self._cancel_power_settle()
        self._forget_feedback((self._on_join, self._off_join))
        self._optimistic_on = on
        self._power_settle_until = time.monotonic() + POWER_SETTLE_SECONDS
        self._power_settle_handle = asyncio.get_running_loop().call_later(
            POWER_SETTLE_SECONDS, self._finish_power_settle
        )

    def _finish_power_settle(self):
        self._power_settle_handle = None
        self._power_settle_until = 0.0
        if self._removed or not self._hub.is_available():
            return
        # Only feedback received since this command may settle it. No new
        # report means retaining optimistic state, never reviving an old cache.
        previous = self._optimistic_on
        self._reconcile_power()
        if previous != self._optimistic_on:
            self._schedule_write()

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
                try:
                    self._target_temp = self._validate_target_temperature(t)
                except ValueError:
                    pass
            f = last.attributes.get("fan_mode")
            if f in self._fan_joins:
                self._fan_mode = f
            c = last.attributes.get("current_temperature")
            if c is not None:
                self._reported_temp = c
        # If already connected, upgrade to live feedback only when the on/off
        # pair is definitive; otherwise retain the restored cold-start state.
        if self._hub.is_available():
            self._feedback_levels = {
                int(key[1:]): self._hub.get_digital(int(key[1:]))
                for key in self._callback_joins()
                if key.startswith("d") and self._hub.has_digital(int(key[1:]))
            }
            power = self._feedback_is_on()
            if power is not None:
                self._optimistic_on = power
            fb = self._feedback_mode()
            if fb is not None:
                self._optimistic_mode = fb
            if (
                self._set_temp_join is not None
                and self._hub.has_analog(self._set_temp_join)
            ):
                self._target_temp = self._hub.get_analog(self._set_temp_join)
            self._reconcile_room_temp()
            fan = self._feedback_fan()
            if fan is not None:
                self._fan_mode = fan

    async def async_will_remove_from_hass(self):
        self._cancel_power_settle()
        await super().async_will_remove_from_hass()

    def _reconcile_room_temp(self):
        """Accept known room temperature, including zero, using one filter."""
        if (
            self._reg_temp_join is None
            or not self._hub.has_analog(self._reg_temp_join)
        ):
            return False
        value = self._hub.get_analog(self._reg_temp_join)
        if (
            self._reported_temp is not None
            and abs(value - self._reported_temp) < TEMP_REPORT_THRESHOLD
        ):
            return False
        self._reported_temp = value
        return True

    async def process_callback(self, cbtype, value):
        if self._removed:
            return
        if cbtype == AVAILABLE_KEY:
            if value == "False":
                self._cancel_power_settle()
                self._power_settle_until = 0.0
                self._feedback_levels.clear()
            # Reconnecting does not make the hub's retained cache fresh.
            self._schedule_write()
            return
        write = False
        if (
            self._set_temp_join is not None
            and cbtype == f"a{self._set_temp_join}"
            and self._hub.has_analog(self._set_temp_join)
        ):
            self._accept_feedback(
                "_target_temp", self._hub.get_analog(self._set_temp_join),
                "target temperature",
            )
            write = True
        if cbtype == self._reg_temp_cbtype:
            write = self._reconcile_room_temp() or write
        if cbtype.startswith("d") and cbtype in self._callback_joins():
            join = int(cbtype[1:])
            self._feedback_levels[join] = value == "1"
            if join in (self._on_join, self._off_join):
                self._reconcile_power()
            if join in self._mode_joins.values():
                mode = self._feedback_mode()
                if mode is not None:
                    self._accept_feedback("_optimistic_mode", mode, "mode")
            if join in self._fan_joins.values():
                fan = self._feedback_fan()
                if fan is not None:
                    self._accept_feedback("_fan_mode", fan, "fan")
            write = True
        if write:
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
        async with self._command_lock:
            if self._removed:
                return
            _LOGGER.debug("%s: turn on requested", self._attr_name)
            self._begin_power_command(True)
            self.async_write_ha_state()
            await self._pulse(self._on_join)

    async def async_turn_off(self):
        async with self._command_lock:
            if self._removed:
                return
            _LOGGER.debug("%s: turn off requested", self._attr_name)
            self._begin_power_command(False)
            self.async_write_ha_state()
            # Leave mode joins alone so the unit remembers its running mode.
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
        async with self._command_lock:
            if self._removed:
                return
            _LOGGER.debug("%s: mode %s requested", self._attr_name, hvac_mode)
            # The lock covers both parts: no later mode/off command may pass
            # this command while its power-on pulse is suspended.
            if not self._optimistic_on:
                self._begin_power_command(True)
                await self._pulse(self._on_join)
                if self._removed:
                    return
            self._forget_feedback(self._mode_joins.values())
            self._optimistic_mode = hvac_mode
            self.async_write_ha_state()
            set_one_clear_others(self._hub, self._mode_joins.values(), join)

    async def async_set_fan_mode(self, fan_mode):
        target = self._fan_joins.get(fan_mode)
        if target is None:
            return
        async with self._command_lock:
            if self._removed:
                return
            _LOGGER.debug("%s: fan %s requested", self._attr_name, fan_mode)
            self._forget_feedback(self._fan_joins.values())
            self._fan_mode = fan_mode
            self.async_write_ha_state()
            set_one_clear_others(self._hub, self._fan_joins.values(), target)

    def _validate_target_temperature(self, value):
        """Validate before changing optimistic state or sending a command."""
        try:
            value = whole_number(value)
        except vol.Invalid as err:
            raise ValueError(
                f"Crestron temperature must be a whole number; got {value!r}"
            ) from err
        if not self._attr_min_temp <= value <= self._attr_max_temp:
            raise ValueError(
                f"temperature {value} is outside "
                f"{self._attr_min_temp}..{self._attr_max_temp} °C"
            )
        return value

    async def async_set_temperature(self, **kwargs):
        if ATTR_TEMPERATURE not in kwargs or self._set_temp_join is None:
            return
        temp = self._validate_target_temperature(kwargs[ATTR_TEMPERATURE])
        async with self._command_lock:
            if self._removed:
                return
            _LOGGER.debug("%s: temperature %s requested", self._attr_name, temp)
            self._target_temp = temp
            self.async_write_ha_state()
            self._hub.set_analog(self._set_temp_join, temp)
