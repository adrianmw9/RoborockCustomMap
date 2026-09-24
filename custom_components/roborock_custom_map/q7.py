"""Roborock Custom Map entities for B01 Q7 devices.

Requires a Home Assistant version whose Roborock integration supports Q7
devices; importing this module raises ImportError otherwise.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from google.protobuf.message import DecodeError
from roborock.exceptions import RoborockException
from roborock.map.proto.b01_scmap_pb2 import RobotMap  # type: ignore[attr-defined]

from homeassistant.components.image import ImageEntity
from homeassistant.components.roborock.coordinator import (
    RoborockB01Q7UpdateCoordinator,
)
from homeassistant.components.roborock.entity import RoborockCoordinatedEntityB01Q7
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .entity import MapRotationImageMixin, MapRotationSelectMixin, png_dimensions

_LOGGER = logging.getLogger(__name__)

# Q7 devices only serve their current map, so each device has a single map entity.
MAP_NAME = "Map"

# The core integration never fetches Q7 maps; the device only pushes them while
# cleaning, so poll for the idle map at this interval.
MAP_REFRESH_INTERVAL = timedelta(minutes=10)

# SCMap header coordinates are in metres; calibration uses millimetres like V1.
_MM_PER_METRE = 1000


def supports_map(coordinator: RoborockB01Q7UpdateCoordinator) -> bool:
    """Return if the installed python-roborock can fetch Q7 maps."""
    return getattr(coordinator.api, "map_content", None) is not None


def _rotation_key(coordinator: RoborockB01Q7UpdateCoordinator) -> str:
    return f"{coordinator.duid_slug}_q7"


def _calibration_points(raw: bytes, image_size: tuple[int, int]) -> list[dict[str, Any]]:
    """Three vacuum/map point pairs describing the transform, for map cards.

    The grid spans the header's min/max bounds at ``resolution`` metres per
    cell, with row 0 at minY; the rendered image is flipped so +Y is up.
    """
    parsed = RobotMap()
    try:
        parsed.ParseFromString(raw)
    except DecodeError:
        return []
    if not parsed.HasField("mapHead"):
        return []
    head = parsed.mapHead
    if not head.sizeX or not head.sizeY or head.resolution <= 0:
        return []

    pixels_per_cell = image_size[0] / head.sizeX
    points = []
    for x, y in ((0, 0), (1000, 0), (0, 1000)):
        col = (x / _MM_PER_METRE - head.minX) / head.resolution
        row = (y / _MM_PER_METRE - head.minY) / head.resolution
        points.append(
            {
                "vacuum": {"x": x, "y": y},
                "map": {
                    "x": col * pixels_per_cell,
                    "y": (head.sizeY - row) * pixels_per_cell,
                },
            }
        )
    return points


class RoborockQ7Map(MapRotationImageMixin, RoborockCoordinatedEntityB01Q7, ImageEntity):
    """The current map of a Q7 device, with rotation and calibration."""

    _attr_has_entity_name = True
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q7UpdateCoordinator,
    ) -> None:
        """Initialize a Roborock Q7 map."""
        RoborockCoordinatedEntityB01Q7.__init__(
            self, f"{coordinator.duid_slug}_custom_map", coordinator
        )
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.rotation_key = _rotation_key(coordinator)
        self._map_list_trait = coordinator.api.map
        self._map_trait = coordinator.api.map_content
        self._attr_name = f"{MAP_NAME}_custom"

        self.cached_map: bytes | None = None
        self._raw_image_size: tuple[int, int] | None = None
        self._calibration_raw: bytes | None = None
        self._calibration: list[dict[str, Any]] = []

    async def async_added_to_hass(self) -> None:
        """Register a trait listener for pushed maps and poll for the idle map."""
        await super().async_added_to_hass()
        self._async_listen_rotation()
        self.async_on_remove(
            self._map_trait.add_update_listener(self._handle_map_update)
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh_map, MAP_REFRESH_INTERVAL
            )
        )
        self._handle_map_update()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_map(),
            name=f"roborock_custom_map_q7_refresh_{self.coordinator.duid}",
        )

    async def _async_refresh_map(self, _now: Any = None) -> None:
        """Fetch the current map from the device."""
        try:
            await self._map_list_trait.refresh()
            await self._map_trait.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to fetch Q7 map: %s", err)
            return
        self._handle_map_update()

    @callback
    def _handle_map_update(self) -> None:
        """Cache the latest map if its content changed."""
        image_content = self._map_trait.image_content
        if image_content is None or image_content == self.cached_map:
            return
        self.cached_map = image_content
        self._raw_image_size = png_dimensions(image_content)
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Get the image (with optional rotation)."""
        if self.cached_map is None:
            return None
        return await self._async_rotated_image(self.cached_map)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for map card usage (rotation-aware calibration)."""
        raw = self._map_trait.raw_api_response
        if raw is None or self._raw_image_size is None:
            return {}

        if raw is not self._calibration_raw:
            self._calibration_raw = raw
            self._calibration = _calibration_points(raw, self._raw_image_size)
        if not self._calibration:
            return {}

        # The Q7 map has no room geometry, only room names.
        return {
            "calibration_points": self._rotate_calibration(self._calibration),
            "rooms": {},
            "zones": None,
        }


class RoborockQ7MapRotationSelect(
    RoborockCoordinatedEntityB01Q7, MapRotationSelectMixin
):
    """Select entity to control Q7 map rotation."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q7UpdateCoordinator,
    ) -> None:
        """Initialize rotation select."""
        RoborockCoordinatedEntityB01Q7.__init__(
            self, f"{coordinator.duid_slug}_map_rotation_q7", coordinator
        )
        self._init_rotation(config_entry, _rotation_key(coordinator), MAP_NAME)
