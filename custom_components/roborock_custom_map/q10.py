"""Roborock Custom Map entities for B01 Q10 devices.

Requires a Home Assistant version whose Roborock integration supports Q10
devices; importing this module raises ImportError otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP
from roborock.data.b01_q10.b01_q10_containers import Q10RoborockPoint
from roborock.exceptions import RoborockException
from roborock.map.b01_grid_layers import GridCalibration
from roborock.map.b01_q10_map_parser import (
    Q10MapPacket,
    Q10MapPacketKind,
    Q10TracePacket,
)
from roborock.map.b01_q10_render import (
    Q10MapOverlays,
    render_q10_map,
    solve_q10_calibration,
)
from roborock.map.map_parser import MapParserConfig

from homeassistant.components.image import ImageEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.roborock.coordinator import (
    RoborockB01Q10UpdateCoordinator,
)
from homeassistant.components.roborock.entity import RoborockCoordinatedEntityB01Q10
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import MapRotationImageMixin, MapRotationSelectMixin, png_dimensions

_LOGGER = logging.getLogger(__name__)

# The Q10 pushes its current map and can also provide read-only previews of its
# saved maps. Keep the live entity for backwards compatibility and add one
# stable image entity per saved map when the installed python-roborock exposes
# the saved-map trait.
MAP_NAME = "Map"

# Header resolution is in centimetres per grid pixel while zone/erase vectors
# use 5 mm units, so one header unit is two vector units.
_VECTOR_UNITS_PER_HEADER_RESOLUTION_UNIT = 2
# Grid cell values >= this are walls/background; room cells are room_id * 4.
_WALL_THRESHOLD = 240


def supports_map(coordinator: RoborockB01Q10UpdateCoordinator) -> bool:
    """Return if the installed python-roborock exposes the Q10 map."""
    return hasattr(getattr(coordinator.api, "map", None), "image_content")


def _rotation_key(coordinator: RoborockB01Q10UpdateCoordinator) -> str:
    return f"{coordinator.duid_slug}_q10"


def _saved_rotation_key(
    coordinator: RoborockB01Q10UpdateCoordinator, map_id: str
) -> str:
    return f"{coordinator.duid_slug}_q10_{map_id}"


def _saved_map_name(map_id: str, map_name: str | None) -> str:
    return map_name or f"Map {map_id}"


def _saved_map_list(maps_trait: Any) -> list[Any]:
    """Return saved maps across python-roborock API versions."""
    if hasattr(maps_trait, "map_list"):
        return list(maps_trait.map_list)
    multi_map = getattr(maps_trait, "multi_map", None)
    if (
        multi_map is None
        or getattr(multi_map, "op", None) != "list"
        or getattr(multi_map, "result", None) != 1
    ):
        return []
    return list(getattr(multi_map, "data", []))


async def _async_refresh_saved_map_detail(maps_trait: Any, map_id: str) -> None:
    """Request a read-only saved-map preview across library versions."""
    if hasattr(maps_trait, "refresh_detail"):
        await maps_trait.refresh_detail(map_id)
        return
    # python-roborock 7.12 (bundled with HA 2026.10) parses saved-map detail
    # packets but predates the public helper. Q10 firmware's `select` operation
    # requests the same read-only preview and does not apply/relocate the map.
    await maps_trait._command.send(  # noqa: SLF001
        B01_Q10_DP.COMMON,
        {
            str(B01_Q10_DP.MULTI_MAP.code): {
                "op": "select",
                "id": map_id,
            }
        },
    )


async def _async_set_current_saved_map(maps_trait: Any, map_id: str) -> None:
    """Apply a saved map across python-roborock API versions."""
    if map_id not in {str(map_info.id) for map_info in _saved_map_list(maps_trait)}:
        raise RoborockException(f"Unknown Q10 saved-map ID: {map_id}")
    if hasattr(maps_trait, "set_current_map"):
        await maps_trait.set_current_map(map_id)
        return
    await maps_trait._command.send(  # noqa: SLF001
        B01_Q10_DP.COMMON,
        {
            str(B01_Q10_DP.MULTI_MAP.code): {
                "op": "apply",
                "id": map_id,
            }
        },
    )


def _vector_calibration(
    packet: Q10MapPacket, trace: Q10TracePacket | None
) -> GridCalibration | None:
    """World (5 mm vector units) to grid-pixel transform for the map packet.

    Uses the fixed origin from the map header when present, otherwise the
    transform fitted to the cleaning trace (2.5 mm units, halved).
    """
    header = packet.header_calibration
    if (
        header is not None
        and header.resolution > 0
        and (origin := header.origin_pixels()) is not None
    ):
        return GridCalibration(
            resolution=header.resolution * _VECTOR_UNITS_PER_HEADER_RESOLUTION_UNIT,
            origin_x=origin[0],
            origin_y=origin[1],
        )
    if (trace_calibration := solve_q10_calibration(packet, trace)) is None:
        return None
    return GridCalibration(
        resolution=trace_calibration.resolution / 2,
        origin_x=trace_calibration.origin_x,
        origin_y=trace_calibration.origin_y,
        y_sign=trace_calibration.y_sign,
    )


def _calibration_points(
    calibration: GridCalibration, pixels_per_cell: float
) -> list[dict[str, Any]]:
    """Three vacuum/map point pairs describing the transform, for map cards."""
    points = []
    for x, y in ((0, 0), (1000, 0), (0, 1000)):
        px, py = calibration.world_to_pixel(x, y)
        vacuum_point = Q10RoborockPoint.from_vector(x, y)
        points.append(
            {
                "vacuum": {"x": vacuum_point.x, "y": vacuum_point.y},
                "map": {"x": px * pixels_per_cell, "y": py * pixels_per_cell},
            }
        )
    return points


def _rooms(
    packet: Q10MapPacket, calibration: GridCalibration
) -> dict[int, dict[str, Any]]:
    """Room bounding boxes in vacuum coordinates."""
    bounds: dict[int, list[int]] = {}
    width = packet.width
    for index, value in enumerate(packet.grid):
        if not 0 < value < _WALL_THRESHOLD:
            continue
        x, y = index % width, index // width
        if (b := bounds.get(value)) is None:
            bounds[value] = [x, y, x, y]
        else:
            b[0] = min(b[0], x)
            b[1] = min(b[1], y)
            b[2] = max(b[2], x)
            b[3] = max(b[3], y)

    rooms = {}
    for room in packet.rooms:
        if (b := bounds.get(room.pixel_value)) is None:
            continue
        ax, ay = calibration.pixel_to_world(b[0], b[1])
        bx, by = calibration.pixel_to_world(b[2] + 1, b[3] + 1)
        first_corner = Q10RoborockPoint.from_vector(ax, ay)
        second_corner = Q10RoborockPoint.from_vector(bx, by)
        x0, x1 = sorted((first_corner.x, second_corner.x))
        y0, y1 = sorted((first_corner.y, second_corner.y))
        rooms[room.id] = {
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "number": room.id,
            "name": room.name,
            "pos_x": (x0 + x1) / 2,
            "pos_y": (y0 + y1) / 2,
        }
    return rooms


class RoborockQ10Map(MapRotationImageMixin, RoborockCoordinatedEntityB01Q10, ImageEntity):
    """The current map of a Q10 device, with rotation and calibration."""

    _attr_has_entity_name = True
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q10UpdateCoordinator,
        async_add_entities: AddConfigEntryEntitiesCallback | None = None,
    ) -> None:
        """Initialize a Roborock Q10 map."""
        RoborockCoordinatedEntityB01Q10.__init__(
            self, f"{coordinator.duid_slug}_custom_map", coordinator
        )
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.rotation_key = _rotation_key(coordinator)
        self._map_trait = coordinator.api.map
        self._maps_trait = getattr(coordinator.api, "maps", None)
        self._async_add_entities = async_add_entities
        self._saved_map_ids: set[str] = set()
        self._attr_name = f"{MAP_NAME}_custom"

        self.cached_map: bytes | None = None
        self._current_packet: Q10MapPacket | None = None
        self._current_trace: Q10TracePacket | None = None
        self._raw_image_size: tuple[int, int] | None = None
        self._rooms_packet: Q10MapPacket | None = None
        self._rooms_calibration: GridCalibration | None = None
        self._cached_rooms: dict[int, dict[str, Any]] = {}

    async def async_added_to_hass(self) -> None:
        """Register a trait listener for push-based map updates."""
        await super().async_added_to_hass()
        self._async_listen_rotation()
        self.async_on_remove(
            self._map_trait.add_update_listener(self._handle_map_update)
        )
        if self._maps_trait is not None and hasattr(
            self._maps_trait, "add_update_listener"
        ):
            self.async_on_remove(
                self._maps_trait.add_update_listener(self._handle_saved_maps_update)
            )
            self._handle_saved_maps_update()
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_refresh_saved_maps(),
                name=f"roborock_custom_map_q10_saved_maps_{self.coordinator.duid}",
            )
        # Pick up a map that was pushed before the entity was added.
        self._handle_map_update()
        # Request the idle map after startup. Without this, a freshly restarted
        # Home Assistant has no calibration until the robot happens to push a
        # map update (normally when a cleaning job starts).
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_map(),
            name=f"roborock_custom_map_q10_refresh_{self.coordinator.duid}",
        )

    async def _async_refresh_saved_maps(self) -> None:
        """Request the Q10 saved-map list without changing the active map."""
        try:
            await self._maps_trait.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to fetch Q10 saved-map list: %s", err)

    @callback
    def _handle_saved_maps_update(self) -> None:
        """Create image entities for newly discovered saved maps."""
        if self._async_add_entities is None or self._maps_trait is None:
            return
        new_entities = []
        for map_info in _saved_map_list(self._maps_trait):
            map_id = str(map_info.id)
            if map_id in self._saved_map_ids:
                continue
            self._saved_map_ids.add(map_id)
            new_entities.append(
                RoborockQ10SavedMap(
                    self.config_entry,
                    self.coordinator,
                    map_id,
                    map_info.name,
                )
            )
        if new_entities:
            self._async_add_entities(new_entities)

    async def _async_refresh_map(self) -> None:
        """Request the current map without starting or relocating the robot."""
        try:
            await self._map_trait.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to fetch Q10 map: %s", err)
            return
        # The response is normally delivered by the registered listener. This
        # also handles implementations that update the trait before returning.
        self._handle_map_update()

    @callback
    def _handle_map_update(self) -> None:
        """Cache the newly pushed map if its content changed."""
        packet: Q10MapPacket | None = getattr(self._map_trait, "_map_packet", None)
        if packet is not None and packet.kind is Q10MapPacketKind.SAVED_MAP_DETAIL:
            return
        image_content = self._map_trait.image_content
        if image_content is None or image_content == self.cached_map:
            return
        self._current_packet = packet
        self._current_trace = getattr(self._map_trait, "_trace_packet", None)
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
        # The map trait exposes the rendered image but not the packets needed
        # to place things on it.
        packet = self._current_packet
        trace = self._current_trace
        if (
            packet is None
            or self._raw_image_size is None
            or (calibration := _vector_calibration(packet, trace)) is None
        ):
            return {}

        if packet is not self._rooms_packet or calibration != self._rooms_calibration:
            self._rooms_packet = packet
            self._rooms_calibration = calibration
            self._cached_rooms = _rooms(packet, calibration)

        pixels_per_cell = self._raw_image_size[0] / packet.width
        return {
            "calibration_points": self._rotate_calibration(
                _calibration_points(calibration, pixels_per_cell)
            ),
            "rooms": self._cached_rooms,
            "zones": None,
        }


class RoborockQ10SavedMap(
    MapRotationImageMixin, RoborockCoordinatedEntityB01Q10, ImageEntity
):
    """A read-only saved-map preview exposed by Q10 firmware."""

    _attr_has_entity_name = True
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q10UpdateCoordinator,
        map_id: str,
        map_name: str | None,
    ) -> None:
        """Initialize a saved Q10 map preview."""
        RoborockCoordinatedEntityB01Q10.__init__(
            self, f"{coordinator.duid_slug}_custom_map_{map_id}", coordinator
        )
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.map_id = map_id
        self.rotation_key = _saved_rotation_key(coordinator, map_id)
        self._maps_trait = coordinator.api.maps
        self._map_trait = coordinator.api.map
        self._map_config = getattr(
            coordinator.api.map, "_config", MapParserConfig()
        )
        self._attr_name = f"{_saved_map_name(map_id, map_name)}_custom"

        self.cached_map: bytes | None = None
        self._packet: Q10MapPacket | None = None
        self._raw_image_size: tuple[int, int] | None = None
        self._cached_rooms: dict[int, dict[str, Any]] = {}

    async def async_added_to_hass(self) -> None:
        """Listen for preview packets and request this saved map."""
        await super().async_added_to_hass()
        self._async_listen_rotation()
        self.async_on_remove(
            self._maps_trait.add_update_listener(self._handle_map_update)
        )
        # python-roborock 7.12 routes saved-map detail packets through the live
        # map trait. Newer versions expose them on the saved-maps trait.
        self.async_on_remove(
            self._map_trait.add_update_listener(self._handle_map_update)
        )
        self._handle_map_update()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_map(),
            name=(
                "roborock_custom_map_q10_saved_map_"
                f"{self.coordinator.duid}_{self.map_id}"
            ),
        )

    async def _async_refresh_map(self) -> None:
        """Request a read-only saved-map preview."""
        try:
            await _async_refresh_saved_map_detail(self._maps_trait, self.map_id)
        except RoborockException as err:
            _LOGGER.debug("Failed to fetch Q10 saved map %s: %s", self.map_id, err)

    @callback
    def _handle_map_update(self) -> None:
        """Render the preview packet matching this entity."""
        packet = getattr(self._maps_trait, "detail_packet", None)
        detail_map_id = getattr(self._maps_trait, "detail_map_id", None)
        if packet is None:
            packet = getattr(self._map_trait, "_map_packet", None)
            detail_map_id = getattr(packet, "map_id", None)
        if (
            packet is None
            or packet.kind is not Q10MapPacketKind.SAVED_MAP_DETAIL
            or str(detail_map_id) != self.map_id
            or packet is self._packet
        ):
            return
        try:
            image_content = render_q10_map(
                packet,
                None,
                Q10MapOverlays(),
                config=self._map_config,
            )
        except RoborockException as err:
            _LOGGER.debug("Failed to render Q10 saved map %s: %s", self.map_id, err)
            return

        self._packet = packet
        self.cached_map = image_content
        self._raw_image_size = png_dimensions(image_content)
        if (calibration := _vector_calibration(packet, None)) is not None:
            self._cached_rooms = _rooms(packet, calibration)
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_restore_live_map(),
            name=(
                "roborock_custom_map_q10_restore_live_map_"
                f"{self.coordinator.duid}_{self.map_id}"
            ),
        )

    async def _async_restore_live_map(self) -> None:
        """Restore the live-map trait after a legacy preview response."""
        # Newer python-roborock versions route previews only to MapsTrait, so
        # there is nothing to restore. In 7.12 the packet temporarily replaces
        # MapContentTrait's source packet; REQUEST_DPS safely restores it.
        if hasattr(self._maps_trait, "detail_packet"):
            return
        try:
            await self._map_trait.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to restore Q10 live map after preview: %s", err)

    async def async_image(self) -> bytes | None:
        """Return the saved preview with its configured rotation."""
        if self.cached_map is None:
            await self._async_refresh_map()
        if self.cached_map is None:
            return None
        return await self._async_rotated_image(self.cached_map)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose calibration and rooms for map cards."""
        packet = self._packet
        if (
            packet is None
            or self._raw_image_size is None
            or (calibration := _vector_calibration(packet, None)) is None
        ):
            return {"map_id": self.map_id}

        pixels_per_cell = self._raw_image_size[0] / packet.width
        return {
            "map_id": self.map_id,
            "calibration_points": self._rotate_calibration(
                _calibration_points(calibration, pixels_per_cell)
            ),
            "rooms": self._cached_rooms,
            "zones": None,
        }


class RoborockQ10MapRotationSelect(
    RoborockCoordinatedEntityB01Q10, MapRotationSelectMixin
):
    """Select entity to control Q10 map rotation."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q10UpdateCoordinator,
        async_add_entities: AddConfigEntryEntitiesCallback | None = None,
    ) -> None:
        """Initialize rotation select."""
        RoborockCoordinatedEntityB01Q10.__init__(
            self, f"{coordinator.duid_slug}_map_rotation_q10", coordinator
        )
        self._init_rotation(config_entry, _rotation_key(coordinator), MAP_NAME)
        self._maps_trait = getattr(coordinator.api, "maps", None)
        self._async_add_entities = async_add_entities
        self._saved_map_ids: set[str] = set()
        self._map_select_added = False

    async def async_added_to_hass(self) -> None:
        """Discover saved maps for rotation and active-map selection."""
        await super().async_added_to_hass()
        if self._maps_trait is None or not hasattr(
            self._maps_trait, "add_update_listener"
        ):
            return
        self.async_on_remove(
            self._maps_trait.add_update_listener(self._handle_saved_maps_update)
        )
        self._handle_saved_maps_update()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_saved_maps(),
            name=f"roborock_custom_map_q10_saved_map_selects_{self.coordinator.duid}",
        )

    async def _async_refresh_saved_maps(self) -> None:
        """Request the list used by the map and rotation selects."""
        try:
            await self._maps_trait.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to fetch Q10 saved-map list: %s", err)

    @callback
    def _handle_saved_maps_update(self) -> None:
        """Add map selector and one rotation select per saved map."""
        if self._async_add_entities is None or self._maps_trait is None:
            return
        map_list = _saved_map_list(self._maps_trait)
        if not map_list:
            return

        new_entities = []
        if not self._map_select_added:
            self._map_select_added = True
            new_entities.append(
                RoborockQ10SavedMapSelect(self.coordinator)
            )
        for map_info in map_list:
            map_id = str(map_info.id)
            if map_id in self._saved_map_ids:
                continue
            self._saved_map_ids.add(map_id)
            new_entities.append(
                RoborockQ10SavedMapRotationSelect(
                    self.config_entry,
                    self.coordinator,
                    map_id,
                    map_info.name,
                )
            )
        if new_entities:
            self._async_add_entities(new_entities)


class RoborockQ10SavedMapRotationSelect(
    RoborockCoordinatedEntityB01Q10, MapRotationSelectMixin
):
    """Rotation control for one saved Q10 map preview."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q10UpdateCoordinator,
        map_id: str,
        map_name: str | None,
    ) -> None:
        """Initialize saved-map rotation."""
        RoborockCoordinatedEntityB01Q10.__init__(
            self,
            f"{coordinator.duid_slug}_map_rotation_q10_{map_id}",
            coordinator,
        )
        self._init_rotation(
            config_entry,
            _saved_rotation_key(coordinator, map_id),
            _saved_map_name(map_id, map_name),
        )


class RoborockQ10SavedMapSelect(
    RoborockCoordinatedEntityB01Q10, SelectEntity
):
    """Select the active saved map on a Q10 device."""

    _attr_has_entity_name = True
    _attr_name = "Selected map"

    def __init__(self, coordinator: RoborockB01Q10UpdateCoordinator) -> None:
        """Initialize active-map selection."""
        RoborockCoordinatedEntityB01Q10.__init__(
            self, f"{coordinator.duid_slug}_selected_map_q10", coordinator
        )
        self._maps_trait = coordinator.api.maps
        self._option_to_id: dict[str, str] = {}
        self._selected_map_id: str | None = None
        self._sync_options()

    async def async_added_to_hass(self) -> None:
        """Track saved-map list changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._maps_trait.add_update_listener(self._handle_maps_update)
        )
        self._handle_maps_update()

    @callback
    def _handle_maps_update(self) -> None:
        self._sync_options()
        self.async_write_ha_state()

    def _sync_options(self) -> None:
        """Build stable, unique user-facing options from the map list."""
        option_to_id: dict[str, str] = {}
        for map_info in _saved_map_list(self._maps_trait):
            map_id = str(map_info.id)
            base = _saved_map_name(map_id, map_info.name)
            option = base
            if option in option_to_id:
                option = f"{base} ({map_id})"
            option_to_id[option] = map_id
        self._option_to_id = option_to_id
        self._attr_options = list(option_to_id)

        if self._selected_map_id not in option_to_id.values():
            self._selected_map_id = getattr(
                self._maps_trait, "current_map_id", None
            )
        self._attr_current_option = next(
            (
                option
                for option, map_id in option_to_id.items()
                if map_id == self._selected_map_id
            ),
            self._attr_options[0] if self._attr_options else None,
        )

    async def async_select_option(self, option: str) -> None:
        """Apply the selected saved map and refresh the live map entity."""
        if (map_id := self._option_to_id.get(option)) is None:
            raise ValueError(f"Unknown Q10 saved map option: {option}")
        await _async_set_current_saved_map(self._maps_trait, map_id)
        self._selected_map_id = map_id
        self._attr_current_option = option
        self.async_write_ha_state()
        try:
            await self.coordinator.api.map.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to refresh Q10 map after selection: %s", err)
