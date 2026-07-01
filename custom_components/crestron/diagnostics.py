"""Diagnostics support for the Crestron XSIG integration.

Because state sync is push-only (the control system reports on change; HA can
never poll a single join), the biggest operational question when something
looks wrong is "has this join ever been reported, and what does the hub think
its value is?". This download surfaces exactly that: the live connection state
and the full digital/analog/serial caches, alongside the configured-entity
counts. Accessible from the config entry's ⋮ menu → Download diagnostics.
"""

from .const import DOMAIN, HUB_WRAPPER


async def async_get_config_entry_diagnostics(hass, entry):
    hub = hass.data.get(DOMAIN, {}).get(HUB_WRAPPER)
    if hub is None:
        return {"error": "hub not initialised"}
    return hub.diagnostics()
