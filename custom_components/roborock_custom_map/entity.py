"""Shared map rotation behavior for Roborock Custom Map entities."""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, UnidentifiedImageError

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MAP_ROTATION,
    DEFAULT_MAP_ROTATION,
    DOMAIN,
    MAP_ROTATION_OPTIONS,
    SIGNAL_ROTATION_CHANGED,
)

_LOGGER = logging.getLogger(__name__)


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return PNG (width, height) from raw bytes, or None if not a PNG."""
    if len(data) < 24:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def _rotate_point_map_xy(
    x: float, y: float, w: int, h: int, rotation: int
) -> tuple[float, float]:
    """Rotate a point in map pixel space around the image bounds.

    rotation is counter-clockwise (PIL Image.rotate does CCW).
    Uses continuous coordinates (w - x / h - y) to avoid off-by-one issues.
    """
    if rotation == 0:
        return (x, y)
    if rotation == 90:
        # CCW 90: new size (h, w)
        return (y, w - x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        # CCW 270 == CW 90: new size (h, w)
        return (h - y, x)
    return (x, y)


def _rotation_signal(config_entry: ConfigEntry, rotation_key: str) -> str:
    return f"{SIGNAL_ROTATION_CHANGED}_{config_entry.entry_id}_{rotation_key}"


class MapRotationImageMixin:
    """Rotation support for a map image entity.

    Subclasses set ``config_entry``, ``rotation_key`` and ``_raw_image_size``.
    """

    hass: HomeAssistant
    config_entry: ConfigEntry
    rotation_key: str
    _raw_image_size: tuple[int, int] | None
    _attr_image_last_updated: Any

    @callback
    def _async_listen_rotation(self) -> None:
        """Listen for rotation changes from the Select entity."""
        self.async_on_remove(  # type: ignore[attr-defined]
            async_dispatcher_connect(
                self.hass,
                _rotation_signal(self.config_entry, self.rotation_key),
                self._async_handle_rotation_changed,
            )
        )

    @callback
    def _async_handle_rotation_changed(self) -> None:
        """Rotation changed; bump last_updated to bust the image cache."""
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _rotate_image(self, raw: bytes, rotation: int) -> bytes:
        """Rotate image in executor thread."""
        img = Image.open(io.BytesIO(raw))
        img = img.rotate(rotation, expand=True)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def _get_rotation(self) -> int:
        """Get configured rotation for this map from hass.data (set by select entity)."""
        rotation = (
            self.hass.data.get(DOMAIN, {})
            .get(self.config_entry.entry_id, {})
            .get(CONF_MAP_ROTATION, {})
            .get(self.rotation_key, DEFAULT_MAP_ROTATION)
        )

        if rotation not in MAP_ROTATION_OPTIONS:
            _LOGGER.debug(
                "Unsupported map rotation %s, allowed values: %s, falling back to %s",
                rotation,
                MAP_ROTATION_OPTIONS,
                DEFAULT_MAP_ROTATION,
            )
            return DEFAULT_MAP_ROTATION

        return rotation

    async def _async_rotated_image(self, raw: bytes) -> bytes:
        """Return the image rotated by the configured rotation."""
        rotation = self._get_rotation()

        if rotation == DEFAULT_MAP_ROTATION:
            return raw

        try:
            return await self.hass.async_add_executor_job(
                self._rotate_image, raw, rotation
            )
        except (OSError, UnidentifiedImageError) as err:
            _LOGGER.debug(
                "Failed to rotate Roborock map image: %s, returning original image",
                err,
            )
            return raw

    def _rotate_calibration(
        self, calibration: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Rotate ONLY the "map" (pixel-space) side of calibration points.

        Rooms/zones are in vacuum coordinate space and are mapped via calibration.
        """
        rotation = self._get_rotation()
        size = self._raw_image_size
        if rotation == DEFAULT_MAP_ROTATION or size is None:
            return calibration

        w, h = size
        rotated_calibration = []
        for pt in calibration:
            mp = pt.get("map") or {}
            x = mp.get("x")
            y = mp.get("y")

            # If missing/invalid, keep point as-is
            if x is None or y is None:
                rotated_calibration.append(pt)
                continue

            nx, ny = _rotate_point_map_xy(float(x), float(y), w, h, rotation)

            new_pt = dict(pt)
            new_map = dict(mp)
            new_map["x"] = nx
            new_map["y"] = ny
            new_pt["map"] = new_map
            rotated_calibration.append(new_pt)

        return rotated_calibration


class MapRotationSelectMixin(RestoreEntity, SelectEntity):
    """Select entity to control map rotation.

    Combine with a Roborock coordinated entity base, then call
    ``_init_rotation`` from ``__init__``.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "rotation"

    config_entry: ConfigEntry
    rotation_key: str

    def _init_rotation(
        self, config_entry: ConfigEntry, rotation_key: str, map_name: str
    ) -> None:
        self.config_entry = config_entry
        self.rotation_key = rotation_key

        self._attr_name = f"{map_name} rotation"
        self._attr_options = [str(v) for v in MAP_ROTATION_OPTIONS]
        self._attr_current_option = str(DEFAULT_MAP_ROTATION)

    async def async_added_to_hass(self) -> None:
        """Restore previous rotation setting and store in hass.data."""
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state in self._attr_options:
                self._attr_current_option = last.state

        # Persist selection for the image entity to read
        self.hass.data[DOMAIN][self.config_entry.entry_id][CONF_MAP_ROTATION][
            self.rotation_key
        ] = int(self._attr_current_option)

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a rotation option."""
        if option not in self._attr_options:
            return

        self._attr_current_option = option

        self.hass.data[DOMAIN][self.config_entry.entry_id][CONF_MAP_ROTATION][
            self.rotation_key
        ] = int(option)

        # Notify the image entity to bust the cache via image_last_updated bump
        async_dispatcher_send(
            self.hass, _rotation_signal(self.config_entry, self.rotation_key)
        )

        self.async_write_ha_state()
