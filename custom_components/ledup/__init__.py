"""LEDUP lights driven through ESPHome BLE relays."""
from __future__ import annotations

from typing import TYPE_CHECKING

# HA imports stay type-only so the CLI can import .protocol without Home Assistant installed.
if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = 'ledup'
PLATFORMS = ['button']


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
