"""Helpers for discovering Roborock maps across Roborock config entries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from roborock.devices.traits.v1.home import HomeTrait

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Sent by the Roborock integration (HA 2026.7+) when a device finishes setting
# up in the background, after the Roborock config entry is already loaded.
SIGNAL_ROBOROCK_COORDINATOR_ADDED = "roborock_coordinator_added_{}"

type MapEntityFactory = Callable[
    [RoborockDataUpdateCoordinator, HomeTrait, Any], Entity
]
# A factory for devices with a single (current) map; returns None to skip.
type DeviceEntityFactory = Callable[[Any], Entity | None]


@callback
def async_setup_map_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_factory: MapEntityFactory,
    device_factories: Sequence[tuple[type, DeviceEntityFactory]] = (),
) -> None:
    """Create one entity per map for every V1 device.

    ``device_factories`` pairs a coordinator class with a factory for devices
    that expose a single map (B01 Q7/Q10), called once per device.

    Devices and maps may appear after this platform is set up, so this listens
    for new coordinators and for home (map list) updates.
    """
    added: set[tuple[str, int]] = set()
    watched: set[str] = set()
    added_devices: set[str] = set()

    @callback
    def async_add_coordinator(coordinator: Any) -> None:
        for coordinator_cls, device_factory in device_factories:
            if not isinstance(coordinator, coordinator_cls):
                continue
            if coordinator.duid not in added_devices:
                added_devices.add(coordinator.duid)
                if (entity := device_factory(coordinator)) is not None:
                    async_add_entities([entity])
            return

        if not isinstance(coordinator, RoborockDataUpdateCoordinator):
            return
        if (home := coordinator.properties_api.home) is None:
            return

        @callback
        def async_update_maps() -> None:
            new_entities = []
            for map_info in (home.home_map_info or {}).values():
                key = (coordinator.duid, map_info.map_flag)
                if key in added:
                    continue
                added.add(key)
                new_entities.append(entity_factory(coordinator, home, map_info))
            if new_entities:
                async_add_entities(new_entities)

        async_update_maps()

        if coordinator.duid not in watched and hasattr(home, "add_update_listener"):
            watched.add(coordinator.duid)
            config_entry.async_on_unload(home.add_update_listener(async_update_maps))

    for r_entry in config_entry.runtime_data:
        for coordinator in r_entry.runtime_data.values():
            async_add_coordinator(coordinator)

        config_entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                SIGNAL_ROBOROCK_COORDINATOR_ADDED.format(r_entry.entry_id),
                async_add_coordinator,
            )
        )
