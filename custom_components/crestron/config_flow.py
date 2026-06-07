"""Config flow for the Crestron XSIG integration.

Single-instance, YAML-backed: the actual hub + entity definitions live under
the `crestron:` key in configuration.yaml. This flow only creates the config
entry that the platforms attach to (so entities get grouped into HA devices).
The entry is created automatically by importing the YAML, and can also be
added from the UI.
"""

from homeassistant import config_entries

from .const import DOMAIN


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
