"""One config entry per LEDUP product, sent through one ESPHome relay action."""
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import DOMAIN
from .protocol import CATALOG

RELAY_SUFFIX = '_ledup_advertise'  # api action name in esphome/ledup.yaml


class LedupConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        relays = sorted(name for name in self.hass.services.async_services_for_domain('esphome') if name.endswith(RELAY_SUFFIX))
        if not relays:
            return self.async_abort(reason='no_relays')
        if user_input is not None:
            await self.async_set_unique_id(user_input['product'])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"LEDUP {user_input['product']}", data=user_input)
        return self.async_show_form(step_id='user', data_schema=vol.Schema({
            vol.Required('product'): SelectSelector(SelectSelectorConfig(options=list(CATALOG), mode=SelectSelectorMode.DROPDOWN)),
            vol.Required('relay'): SelectSelector(SelectSelectorConfig(options=relays)),
        }))
