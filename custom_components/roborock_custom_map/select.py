"""Support for Roborock map rotation select entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.components.roborock.entity import RoborockCoordinatedEntityV1
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import MapRotationSelectMixin
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

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up rotation Select entities (one per map)."""
    async_setup_map_entities(
        hass,
        config_entry,
        async_add_entities,
        lambda coord, home, map_info: RoborockMapRotationSelect(
            config_entry=config_entry,
            unique_id=f"{coord.duid_slug}_map_rotation_{map_info.map_flag}",
            coordinator=coord,
            map_flag=map_info.map_flag,
            map_name=map_info.name,
        ),
        _device_factories(config_entry, async_add_entities),
    )


def _device_factories(
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> list[tuple[type, Any]]:
    factories: list[tuple[type, Any]] = []
    if q7 is not None:
        factories.append(
            (
                q7.RoborockB01Q7UpdateCoordinator,
                lambda coord: q7.RoborockQ7MapRotationSelect(config_entry, coord)
                if q7.supports_map(coord)
                else None,
            )
        )
    if q10 is not None:
        factories.append(
            (
                q10.RoborockB01Q10UpdateCoordinator,
                lambda coord: q10.RoborockQ10MapRotationSelect(
                    config_entry, coord, async_add_entities
                )
                if q10.supports_map(coord)
                else None,
            )
        )
    return factories


class RoborockMapRotationSelect(RoborockCoordinatedEntityV1, MapRotationSelectMixin):
    """Select entity to control map rotation."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        unique_id: str,
        coordinator: RoborockDataUpdateCoordinator,
        map_flag: int,
        map_name: str,
    ) -> None:
        """Initialize rotation select."""
        RoborockCoordinatedEntityV1.__init__(self, unique_id, coordinator)

        self.map_flag = map_flag
        self._init_rotation(
            config_entry,
            f"{coordinator.duid_slug}_{map_flag}",
            map_name or f"Map {map_flag}",
        )
