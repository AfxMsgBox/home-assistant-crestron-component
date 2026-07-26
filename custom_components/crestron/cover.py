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
from .join_commands import pulse_digital

_LOGGER = logging.getLogger(__name__)


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
            vol.Required(CONF_TYPE): cv.string,
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
        keep current. No slider is offered in this mode (SET_POSITION is only
        advertised once a real position has arrived), just open/close/stop.
        The moment the control system starts reporting position, the entity
        upgrades itself to the real value with no config change.
      - Optimistic state is restored across restarts via RestoreEntity.

    Open/close/stop always pulse their joins unconditionally, so control is
    never gated by feedback state (required behaviour per the join table).
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
        self._attr_unique_id = (
            f"crestron_cover_{self._pos_join or self._open_join or self._close_join}"
        )
        self._attr_device_info = device_info(config)
        if config.get(CONF_TYPE) == "curtain":
            self._attr_device_class = CoverDeviceClass.CURTAIN
        else:
            self._attr_device_class = CoverDeviceClass.SHADE
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

    def _callback_joins(self):
        joins = []
        if self._pos_join is not None:
            joins.append(f"a{self._pos_join}")
        for j in (
            self._is_opening_join,
            self._is_closing_join,
            self._is_closed_join,
        ):
            if j is not None:
                joins.append(f"d{j}")
        return joins

    @property
    def supported_features(self):
        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )
        # Only offer the position slider once we have a real position to track;
        # while running on optimistic open/close inference, expose just the
        # open/close/stop buttons (no misleading "set exact %" control).
        if self._has_real_position():
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
        # Legacy is_closed feedback can still give a coarse 0/100.
        if self._is_closed_join is not None:
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
            return self._hub.get_digital(self._is_closed_join)
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def process_callback(self, cbtype, value):
        # A real position report supersedes the optimistic value; drop it so
        # current_cover_position switches to the feedback source cleanly.
        if self._has_real_position():
            self._optimistic_pos = None
        self.async_write_ha_state()

    async def _pulse(self, join):
        await pulse_digital(self._hub, self._pulse_lock, join)

    async def async_set_cover_position(self, **kwargs):
        position = max(0, min(100, int(kwargs["position"])))
        if self._pos_join is not None:
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
