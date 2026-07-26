"""Config flow for the Crestron XSIG integration.

Single-instance, YAML-backed: the actual hub + entity definitions live under
the `crestron:` key in configuration.yaml. This flow only creates the config
entry that the platforms attach to (so entities get grouped into HA devices).
The entry is created automatically by importing the YAML, and can also be
added from the UI.
"""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, HUB_WRAPPER


class CrestronConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crestron XSIG."""

    VERSION = 1

    async def _create(self):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Crestron XSIG", data={})

    async def async_step_import(self, import_data):
        """Create the entry from YAML (`crestron:` in configuration.yaml)."""
        return await self._create()

    async def async_step_user(self, user_input=None):
        """Create the entry from the UI (config still comes from YAML)."""
        return await self._create()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CrestronOptionsFlow()


class CrestronOptionsFlow(config_entries.OptionsFlow):
    """Options: a manual "resync all to_joins" action.

    Config still lives in YAML, so there are no editable settings here — the
    one useful action is forcing HA's known state back onto the control system
    (normally only done on reconnect / 0xFB). Ticking the box and submitting
    triggers the resync; nothing is persisted.
    """

    async def async_step_init(self, user_input=None):
        if user_input is not None and user_input.get("resync_to_joins"):
            hub = self.hass.data.get(DOMAIN, {}).get(HUB_WRAPPER)
            if hub is not None:
                hub.resync_to_joins()
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional("resync_to_joins", default=False): bool}
            ),
        )
