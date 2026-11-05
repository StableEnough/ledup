"""One button per catalog command; a press advertises once through the entry's ESPHome relay."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.singleton import singleton
from homeassistant.helpers.storage import Store

from . import DOMAIN
from .protocol import CATALOG, command_bytes, encode_payload, reserve

# Flags AD + manufacturer AD header (company 0xfff0). The encoder pre-whitens for BLE channel 37 with the
# payload at PDU byte 15 (header 2 + AdvA 6 + these 7), as Android's connectable ads place it; drop the flags and it shifts.
AD_HEADER = b'\x02\x01\x06\x13\xff\xf0\xff'


@singleton(f'{DOMAIN}_counters')
async def _counters(hass: HomeAssistant) -> tuple[Store, dict]:
    """Shared by all entries: the packet counter is global, like the CLI's."""
    store = Store(hass, 1, DOMAIN)
    return store, await store.async_load() or {}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    product = entry.data['product']
    device = DeviceInfo(identifiers={(DOMAIN, product)}, name=entry.title, manufacturer='LEDUP', model=product)
    async_add_entities(LedupButton(entry, device, item) for item in CATALOG[product]['commands'])


class LedupButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, device: DeviceInfo, item: dict) -> None:
        self._product, self._key, self._relay = entry.data['product'], item['key'], entry.data['relay']
        self._attr_name = item['label']
        self._attr_unique_id = f'{self._product}_{self._key}'
        self._attr_device_info = device

    async def async_press(self) -> None:
        await async_send(self.hass, self._relay, self._product, self._key)


async def async_send(hass: HomeAssistant, relay: str, product: str, key: int) -> None:
    # No retries: Switch is a toggle, so a repeat can undo the press.
    store, counters = await _counters(hass)
    packet, click = reserve(counters, product, key)
    await store.async_save(counters)  # counters persist before RF, like the CLI
    payload = encode_payload(command_bytes(CATALOG[product]['page'], key, click), packet)
    await hass.services.async_call('esphome', relay, {'adv': list(AD_HEADER + payload)}, blocking=True)
