"""Platform for Crestron Shades integration."""
import asyncio
import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import CONF_NAME, CONF_TYPE
from homeassistant.helpers.restore_state import RestoreEntity
from .const import (
    CONF_IS_OPENING_JOIN,
    CONF_IS_CLOSING_JOIN,
    CONF_IS_CLOSED_JOIN,
    CONF_OPEN_JOIN,
    CONF_CLOSE_JOIN,
    CONF_STOP_JOIN,
    CONF_POS_JOIN,
)
from .schema import analog_join, digital_join
from .device import device_info
from .entity import CrestronEntity, setup_platform_entities
from .join_commands import paired_feedback, pulse_digital
from .unique_ids import cover_unique_id

_LOGGER = logging.getLogger(__name__)

DEFAULT_COVER_TYPE = "curtain"
_COVER_DEVICE_CLASSES = {
    "awning": CoverDeviceClass.AWNING,
    "blind": CoverDeviceClass.BLIND,
    "curtain": CoverDeviceClass.CURTAIN,
    "damper": CoverDeviceClass.DAMPER,
    "door": CoverDeviceClass.DOOR,
    "garage": CoverDeviceClass.GARAGE,
    "gate": CoverDeviceClass.GATE,
    "shade": CoverDeviceClass.SHADE,
    "shutter": CoverDeviceClass.SHUTTER,
    "window": CoverDeviceClass.WINDOW,
}


def _normalize_cover_type(value):
    """Return a supported HA cover type, defaulting unknown/blank to curtain."""
    normalized = str(value or "").strip().lower()
    if normalized in _COVER_DEVICE_CLASSES:
        return normalized
    return DEFAULT_COVER_TYPE


def _require_drive_join(config):
    has_open = CONF_OPEN_JOIN in config
    has_close = CONF_CLOSE_JOIN in config
    if has_open != has_close:
        raise vol.Invalid(
            "open_join and close_join must be configured together"
        )
    if CONF_POS_JOIN in config or (has_open and has_close):
        return config
    raise vol.Invalid(
        "Either pos_join, or both open_join and close_join, must be configured"
    )


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_TYPE, default=DEFAULT_COVER_TYPE): vol.All(
                cv.string, _normalize_cover_type
            ),
            vol.Optional(CONF_POS_JOIN): analog_join,
            vol.Optional(CONF_OPEN_JOIN): digital_join,
            vol.Optional(CONF_CLOSE_JOIN): digital_join,
            vol.Optional(CONF_IS_OPENING_JOIN): digital_join,
            vol.Optional(CONF_IS_CLOSING_JOIN): digital_join,
            vol.Optional(CONF_IS_CLOSED_JOIN): digital_join,
            vol.Required(CONF_STOP_JOIN): digital_join,
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _require_drive_join,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        setup_platform_entities(hass, "cover", PLATFORM_SCHEMA, CrestronShade)
    )


class CrestronShade(CrestronEntity, CoverEntity, RestoreEntity):
    """A Crestron cover.

    Position handling degrades gracefully depending on what the control system
    actually reports back:
      - If ``pos_join`` carries a real 0–100 position feedback, that is the
        truth and the card shows a draggable position slider.
      - If ``pos_join`` is configured but the control system never pushes it
        (a known Crestron-side gap), we fall back to an *optimistic* position
        inferred from the open/close commands: open -> 100, close -> 0, stop ->
        keep current. The configured position control remains available; until
        real feedback arrives, its requested value is shown as assumed state.
        The moment the control system reports position, that real value wins.
      - Optimistic state is restored across restarts via RestoreEntity.

    Open/close/stop always pulse their joins unconditionally, so control is
    never gated by feedback state. CP4N's stable open/close feedback returns on
    those same command joins; the component does not require extra joins.
    """

    def __init__(self, hub, config):
        self._hub = hub
        self._attr_name = config.get(CONF_NAME)
        self._is_opening_join = config.get(CONF_IS_OPENING_JOIN)
        self._is_closing_join = config.get(CONF_IS_CLOSING_JOIN)
        self._is_closed_join = config.get(CONF_IS_CLOSED_JOIN)
        self._open_join = config.get(CONF_OPEN_JOIN)
        self._close_join = config.get(CONF_CLOSE_JOIN)
        self._stop_join = config.get(CONF_STOP_JOIN)
        self._pos_join = config.get(CONF_POS_JOIN)
        self._pulse_lock = asyncio.Lock()
        self._attr_unique_id = cover_unique_id(config)
        self._attr_device_info = device_info(config)
        cover_type = _normalize_cover_type(config.get(CONF_TYPE))
        self._attr_device_class = _COVER_DEVICE_CLASSES[cover_type]
        # Optimistic position (0/100) used when there is no real position
        # feedback; None until the first command / restore.
        self._optimistic_pos = None

    def _has_real_position(self):
        """True once the control system has actually pushed a position value."""
        return self._pos_join is not None and self._hub.has_analog(self._pos_join)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Restore the pre-restart optimistic position so a command-driven cover
        # doesn't come back as unknown. Real position feedback (if any) takes
        # precedence via current_cover_position, so this only matters while the
        # control system isn't reporting position.
        last = await self.async_get_last_state()
        if last is not None:
            pos = last.attributes.get("current_position")
            if isinstance(pos, (int, float)):
                self._optimistic_pos = max(0, min(100, int(pos)))
        # The hub cache may already contain CP4N's mutually-exclusive open/close
        # feedback before this entity is added. Use it when definitive.
        feedback = self._feedback_is_closed()
        if not self._has_real_position() and feedback is not None:
            self._optimistic_pos = 0 if feedback else 100

    def _callback_joins(self):
        joins = []
        if self._pos_join is not None:
            joins.append(f"a{self._pos_join}")
        for j in (
            # The existing open/close command joins also carry CP4N's stable
            # open/closed feedback; no extra Crestron joins are required.
            self._open_join,
            self._close_join,
            self._is_opening_join,
            self._is_closing_join,
            self._is_closed_join,
        ):
            key = f"d{j}" if j is not None else None
            if key is not None and key not in joins:
                joins.append(key)
        return joins

    def _feedback_is_closed(self):
        """Return definitive CP4N open/close feedback, or None in transition.

        Both joins are required here: unlike a switch, a lone open (or close)
        join says nothing about position once the pulse clears.
        """
        if self._open_join is None or self._close_join is None:
            return None
        return paired_feedback(self._hub, self._close_join, self._open_join)

    @property
    def supported_features(self):
        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )
        # A configured 0–100 position join is a control capability even before
        # CP4N sends its first feedback frame. Until then the state is marked
        # assumed and follows the command optimistically.
        if self._pos_join is not None:
            features |= CoverEntityFeature.SET_POSITION
        return features

    @property
    def assumed_state(self):
        # Without real position feedback the state is inferred from commands, so
        # tell HA not to gate the open/close buttons on it.
        return not self._has_real_position()

    @property
    def current_cover_position(self):
        # Real feedback wins whenever it's available (auto-upgrade if the
        # control system starts reporting position later).
        if self._has_real_position():
            # 0–100 analog (0=closed, 100=open), reported directly — not XSIG
            # full scale. Clamp defensively against stray out-of-range values.
            return max(0, min(100, self._hub.get_analog(self._pos_join)))
        # Optimistic fallback: whatever the last open/close command implied
        # (restored across restarts). None until the first command.
        if self._optimistic_pos is not None:
            return self._optimistic_pos
        feedback = self._feedback_is_closed()
        if feedback is not None:
            return 0 if feedback else 100
        # Legacy is_closed feedback can still give a coarse 0/100 — but only
        # once it has actually been reported.
        if self._is_closed_join is not None and self._hub.has_digital(
            self._is_closed_join
        ):
            return 0 if self._hub.get_digital(self._is_closed_join) else 100
        return None

    @property
    def is_opening(self):
        if self._is_opening_join is None:
            return False
        return self._hub.get_digital(self._is_opening_join)

    @property
    def is_closing(self):
        if self._is_closing_join is None:
            return False
        return self._hub.get_digital(self._is_closing_join)

    @property
    def is_closed(self):
        if self._is_closed_join is not None:
            if not self._hub.has_digital(self._is_closed_join):
                return None  # unreported: unknown, not "open"
            return self._hub.get_digital(self._is_closed_join)
        feedback = self._feedback_is_closed()
        if feedback is not None:
            return feedback
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def process_callback(self, cbtype, value):
        # A real position report supersedes the optimistic value; drop it so
        # current_cover_position switches to the feedback source cleanly.
        if self._has_real_position():
            self._optimistic_pos = None
        else:
            feedback = self._feedback_is_closed()
            if feedback is not None:
                self._optimistic_pos = 0 if feedback else 100
        self._schedule_write()

    async def _pulse(self, join):
        await pulse_digital(self._hub, self._pulse_lock, join)

    async def async_set_cover_position(self, **kwargs):
        position = max(0, min(100, int(kwargs["position"])))
        if self._pos_join is not None:
            if not self._has_real_position():
                self._optimistic_pos = position
                self.async_write_ha_state()
            # 0–100 directly (0=closed, 100=open), not XSIG full scale.
            self._hub.set_analog(self._pos_join, position)
        elif self._open_join is not None and self._close_join is not None:
            if position > 50:
                await self.async_open_cover(**kwargs)
            else:
                await self.async_close_cover(**kwargs)

    async def async_open_cover(self, **kwargs):
        # Optimistically assume fully open until real feedback says otherwise.
        self._optimistic_pos = 100
        self.async_write_ha_state()
        if self._open_join is not None:
            await self._pulse(self._open_join)
        elif self._pos_join is not None:
            self._hub.set_analog(self._pos_join, 100)

    async def async_close_cover(self, **kwargs):
        self._optimistic_pos = 0
        self.async_write_ha_state()
        if self._close_join is not None:
            await self._pulse(self._close_join)
        elif self._pos_join is not None:
            self._hub.set_analog(self._pos_join, 0)

    async def async_stop_cover(self, **kwargs):
        # Stop can't infer a mid-travel position, so leave the optimistic value
        # as-is (real feedback, when present, still reconciles it).
        await self._pulse(self._stop_join)
        self.async_write_ha_state()
