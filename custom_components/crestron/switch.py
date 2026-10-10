"""Platform for Crestron Switch integration."""

import asyncio
import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
from .join_commands import paired_feedback, pulse_digital
from .unique_ids import switch_unique_id

_LOGGER = logging.getLogger(__name__)

_SWITCH_DEVICE_CLASSES = {
    "outlet": SwitchDeviceClass.OUTLET,
    "switch": SwitchDeviceClass.SWITCH,
}


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
            vol.Optional(CONF_DEVICE_CLASS): vol.All(
                cv.string, vol.In(_SWITCH_DEVICE_CLASSES)
            ),
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
        device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_device_class = (
            _SWITCH_DEVICE_CLASSES[device_class] if device_class else None
        )
        self._optimistic_state = False
        self._pulse_lock = asyncio.Lock()
        self._attr_unique_id = switch_unique_id(config)
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
            is_on, _mode = self._mode_feedback()
            return is_on
        if self._state_join is not None:
            # Unreported = unknown, so the restored/optimistic state stands.
            if not self._hub.has_digital(self._state_join):
                return None
            return self._hub.get_digital(self._state_join)
        if self._switch_join is not None:
            if not self._hub.has_digital(self._switch_join):
                return None
            return self._hub.get_digital(self._switch_join)
        # Pulse mode can still carry feedback on the command joins themselves.
        return paired_feedback(self._hub, self._on_join, self._off_join)

    def _mode_feedback(self):
        """Return ``(is_on, label)`` from mode joins.

        A high join is definitive even while the rest of the initial sync is
        still arriving. All-low is definitive only after every configured join
        has been reported; before that both the switch state and its ``mode``
        attribute must remain unknown/restored rather than claiming "关闭".
        """
        for label, join in self._mode_joins.items():
            if self._hub.get_digital(join):
                return True, label
        if all(self._hub.has_digital(j) for j in self._mode_joins.values()):
            return False, "关闭"
        return None, None

    async def process_callback(self, cbtype, value):
        # Reconcile with CP4N feedback, including feedback returned on the same
        # joins that are pulsed for on/off commands.
        fb = self._feedback_is_on()
        if fb is not None:
            self._optimistic_state = fb
        self._schedule_write()

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
        is_on, mode = self._mode_feedback()
        if is_on is None:
            return None
        return {"mode": mode}

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
