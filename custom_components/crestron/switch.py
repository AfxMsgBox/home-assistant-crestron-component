"""Platform for Crestron Switch integration."""

import asyncio
import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_NAME, CONF_DEVICE_CLASS
from homeassistant.helpers.restore_state import RestoreEntity
from .const import (
    CONF_SWITCH_JOIN,
    CONF_ON_JOIN,
    CONF_OFF_JOIN,
    CONF_STATE_JOIN,
    CONF_MODE_JOINS,
)
from .schema import digital_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .join_commands import pulse_digital

_LOGGER = logging.getLogger(__name__)


def _require_writable_join(config):
    has_on = CONF_ON_JOIN in config
    has_off = CONF_OFF_JOIN in config
    has_direct = CONF_SWITCH_JOIN in config

    if has_on != has_off:
        raise vol.Invalid(
            "on_join and off_join must be configured together (pulse mode)"
        )
    has_pulse = has_on and has_off
    if has_pulse and has_direct:
        raise vol.Invalid(
            "switch_join cannot be combined with on_join/off_join; "
            "use state_join for feedback in pulse mode"
        )
    if not (has_pulse or has_direct):
        raise vol.Invalid(
            "Either switch_join, or both on_join and off_join, must be configured"
        )
    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_SWITCH_JOIN): digital_join,
            vol.Optional(CONF_ON_JOIN): digital_join,
            vol.Optional(CONF_OFF_JOIN): digital_join,
            vol.Optional(CONF_STATE_JOIN): digital_join,
            vol.Optional(CONF_MODE_JOINS): {cv.string: digital_join},
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_writable_join,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        setup_platform_entities(hass, "switch", PLATFORM_SCHEMA, CrestronSwitch)
    )


class CrestronSwitch(CrestronEntity, SwitchEntity, RestoreEntity):
    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._switch_join = config.get(CONF_SWITCH_JOIN)
        self._on_join = config.get(CONF_ON_JOIN)
        self._off_join = config.get(CONF_OFF_JOIN)
        self._state_join = config.get(CONF_STATE_JOIN)
        # {label: digital_join}: on if any join is asserted; active label is
        # surfaced as a read-only "mode" attribute (e.g. AC running mode).
        self._mode_joins = config.get(CONF_MODE_JOINS) or {}
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._optimistic_state = False
        self._pulse_lock = asyncio.Lock()
        uid = self._switch_join or self._state_join or self._on_join
        self._attr_unique_id = f"crestron_switch_{uid}_{self._attr_name}"
        self._attr_device_info = device_info(config)

    def _callback_joins(self):
        if self._mode_joins:
            return [f"d{j}" for j in self._mode_joins.values()]
        if self._state_join is not None:
            return [f"d{self._state_join}"]
        if self._switch_join is not None:
            return [f"d{self._switch_join}"]
        joins = []
        if self._on_join is not None:
            joins.append(f"d{self._on_join}")
        if self._off_join is not None:
            joins.append(f"d{self._off_join}")
        return joins

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Restore first for the cold-start window, then upgrade to definitive
        # live feedback when Crestron has already reported it.
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._optimistic_state = last.state == "on"
        if self._hub.is_available():
            fb = self._feedback_is_on()
            if fb is not None:
                self._optimistic_state = fb

    def _feedback_is_on(self):
        """On-state from feedback joins, or None if this switch has no feedback.

        For the AC power switch the "feedback" is the set of running-mode joins
        (制冷/制热/…): the unit is on iff one of them is asserted.
        """
        if self._mode_joins:
            return any(
                self._hub.get_digital(j) for j in self._mode_joins.values()
            )
        if self._state_join is not None:
            return self._hub.get_digital(self._state_join)
        if self._switch_join is not None:
            return self._hub.get_digital(self._switch_join)
        # Pulse mode can still carry feedback on the command joins themselves.
        # Exactly one asserted is definitive; both low/high is transitional or
        # not-yet-reported, so keep the current optimistic state.
        on = self._on_join is not None and self._hub.get_digital(self._on_join)
        off = self._off_join is not None and self._hub.get_digital(self._off_join)
        if on != off:
            return on
        return None

    async def process_callback(self, cbtype, value):
        # Reconcile with real feedback. Only genuine feedback joins are
        # registered (never the momentary on/off command joins), so this fires
        # on real state transitions — including changes made outside HA — and
        # never spuriously bounces the toggle right after a command.
        fb = self._feedback_is_on()
        if fb is not None:
            self._optimistic_state = fb
        self.async_write_ha_state()

    @property
    def is_on(self):
        # Optimistic-first: reflect the user's last command immediately so the
        # toggle doesn't bounce while the control system's feedback join catches
        # up; process_callback keeps this in sync with the real feedback.
        return self._optimistic_state

    @property
    def extra_state_attributes(self):
        if not self._mode_joins:
            return None
        for label, join in self._mode_joins.items():
            if self._hub.get_digital(join):
                return {"mode": label}
        return {"mode": "关闭"}

    async def _pulse(self, join):
        await pulse_digital(self._hub, self._pulse_lock, join)

    async def async_turn_on(self, **kwargs):
        self._optimistic_state = True
        self.async_write_ha_state()
        if self._on_join is not None:
            await self._pulse(self._on_join)
        else:
            self._hub.set_digital(self._switch_join, True)

    async def async_turn_off(self, **kwargs):
        self._optimistic_state = False
        self.async_write_ha_state()
        if self._off_join is not None:
            await self._pulse(self._off_join)
        else:
            self._hub.set_digital(self._switch_join, False)
