"""Shared helper to group related entities under one HA device.

Any entity whose config carries `device_id` is attached to a device with that
identifier; entities sharing the same `device_id` (e.g. an AC's power switch,
temperature number, fan select and room-temp/mode sensors) appear together as
one device in Home Assistant.
"""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CONF_DEVICE_ID, CONF_DEVICE_NAME


def device_info(config: dict[str, Any]) -> Optional[DeviceInfo]:
    """Build DeviceInfo from config, or None when no device_id is set."""
    device_id = config.get(CONF_DEVICE_ID)
    if not device_id:
        return None
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=config.get(CONF_DEVICE_NAME) or device_id,
    )
