"""Roborock Custom Map integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_MAP_ROTATION, DOMAIN

PLATFORMS = [Platform.IMAGE, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Roborock Custom map from a config entry."""
    roborock_entries = [
        r_entry for r_entry in hass.config_entries.async_entries("roborock")
        if r_entry.state == ConfigEntryState.LOADED
    ]

    @callback
    def unload_this_entry() -> None:
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    if not roborock_entries:
        raise ConfigEntryNotReady("No Roborock entries loaded. Cannot start.")

    for r_entry in roborock_entries:
        r_entry.async_on_unload(unload_this_entry)

    entry.runtime_data = roborock_entries

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(CONF_MAP_ROTATION, {})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded