"""One config entry per LEDUP product, sent through one ESPHome relay action."""
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import DOMAIN
from .button import async_send
from .protocol import CATALOG

RELAY_SUFFIX = '_ledup_advertise'  # api action name in esphome/ledup.yaml
FIND = 'find'  # product select value that starts the probe; never a product ID
PRODUCTS = list(CATALOG)


class LedupConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _relay = ''
    _index = 0

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        relays = sorted(name for name in self.hass.services.async_services_for_domain('esphome') if name.endswith(RELAY_SUFFIX))
        if not relays:
            return self.async_abort(reason='no_relays')
        if user_input is not None:
            if user_input['product'] == FIND:
                self._relay, self._index = user_input['relay'], 0
                return await self.async_step_probe()
            return await self._create(user_input['product'], user_input['relay'])
        options = [SelectOptionDict(value=FIND, label="I don't know: find it by toggling the lights")]
        options += [SelectOptionDict(value=p, label=p) for p in PRODUCTS]
        return self.async_show_form(step_id='user', data_schema=vol.Schema({
            vol.Required('product'): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)),
            vol.Required('relay'): SelectSelector(SelectSelectorConfig(options=relays)),
        }))

    async def async_step_probe(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send Switch for one product, then ask whether the lights toggled."""
        product = PRODUCTS[self._index]
        key = next(c['key'] for c in CATALOG[product]['commands'] if c['label'] == 'Switch')
        await async_send(self.hass, self._relay, product, key)
        return self.async_show_menu(step_id='probe', menu_options=['probe_found', 'probe_next'], description_placeholders={
            'product': product, 'number': str(self._index + 1), 'total': str(len(PRODUCTS))})

    async def async_step_probe_next(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._index += 1
        if self._index == len(PRODUCTS):
            return self.async_abort(reason='not_found')
        return await self.async_step_probe()

    async def async_step_probe_found(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._create(PRODUCTS[self._index], self._relay)

    async def _create(self, product: str, relay: str) -> ConfigFlowResult:
        await self.async_set_unique_id(product)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f'LEDUP {product}', data={'product': product, 'relay': relay})
