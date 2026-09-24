"""Support for Roborock image."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from roborock.devices.traits.v1.home import HomeTrait
from roborock.devices.traits.v1.map_content import MapContent

from homeassistant.components.image import ImageEntity
from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.components.roborock.entity import RoborockCoordinatedEntityV1
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import MapRotationImageMixin, png_dimensions
from .helpers import async_setup_map_entities

# Home Assistant versions without B01 Q7/Q10 support can't import these.
try:
    from . import q7
except ImportError:
    q7 = None
try:
    from . import q10
except ImportError:
    q10 = None

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Roborock image platform."""
    async_setup_map_entities(
        hass,
        config_entry,
        async_add_entities,
        lambda coord, home, map_info: RoborockMap(
            config_entry,
            f"{coord.duid_slug}_custom_map_{map_info.name or f'Map {map_info.map_flag}'}",
            coord,
            home,
            map_info.map_flag,
            map_info.name,
        ),
        _device_factories(config_entry),
    )


def _device_factories(config_entry: ConfigEntry) -> list[tuple[type, Any]]:
    factories: list[tuple[type, Any]] = []
    if q7 is not None:
        factories.append(
            (
                q7.RoborockB01Q7UpdateCoordinator,
                lambda coord: q7.RoborockQ7Map(config_entry, coord)
                if q7.supports_map(coord)
                else None,
            )
        )
    if q10 is not None:
        factories.append(
            (
                q10.RoborockB01Q10UpdateCoordinator,
                lambda coord: q10.RoborockQ10Map(config_entry, coord)
                if q10.supports_map(coord)
                else None,
            )
        )
    return factories


class RoborockMap(MapRotationImageMixin, RoborockCoordinatedEntityV1, ImageEntity):
    """A class to let you visualize the map."""

    _attr_has_entity_name = True
    image_last_updated: datetime
    _attr_name: str

    def __init__(
        self,
        config_entry: ConfigEntry,
        unique_id: str,
        coordinator: RoborockDataUpdateCoordinator,
        home_trait: HomeTrait,
        map_flag: int,
        map_name: str,
    ) -> None:
        """Initialize a Roborock map."""
        RoborockCoordinatedEntityV1.__init__(self, unique_id, coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.map_flag = map_flag
        self.rotation_key = f"{coordinator.duid_slug}_{map_flag}"
        self._home_trait = home_trait

        if not map_name:
            map_name = f"Map {map_flag}"
        self._attr_name = f"{map_name}_custom"

        self.cached_map = b""
        self._raw_image_size: tuple[int, int] | None = None

        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_selected(self) -> bool:
        """Return if this map is the currently selected map."""
        return self.map_flag == self.coordinator.properties_api.maps.current_map

    @property
    def _map_content(self) -> MapContent | None:
        if self._home_trait.home_map_content and (
            map_content := self._home_trait.home_map_content.get(self.map_flag)
        ):
            return map_content
        return None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass load any previously cached maps from disk."""
        await super().async_added_to_hass()

        self._attr_image_last_updated = self.coordinator.last_home_update
        self._async_listen_rotation()

        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update."""
        if (map_content := self._map_content) is None:
            return

        if self.cached_map != map_content.image_content:
            self.cached_map = map_content.image_content
            self._raw_image_size = png_dimensions(self.cached_map)
            self._attr_image_last_updated = self.coordinator.last_home_update

        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Get the image (with optional rotation)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        return await self._async_rotated_image(map_content.image_content)

    @property
    def extra_state_attributes(self):
        """Return extra attributes for map card usage (rotation-aware calibration)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        map_data = map_content.map_data
        if map_data is None:
            return {}

        # Attach room names (same behavior as before)
        if map_data.rooms is not None:
            for room in map_data.rooms.values():
                name = self._home_trait._rooms_trait.room_map.get(room.number)
                room.name = name.name if name else "Unknown"

        return {
            "calibration_points": self._rotate_calibration(map_data.calibration()),
            "rooms": map_data.rooms,
            "zones": map_data.zones,
        }
