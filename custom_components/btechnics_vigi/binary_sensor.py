"""Verbinding per kanaal (camera online volgens de recorder)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coord = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(VigiOnline(coord, d["channel"]) for d in coord.data or [])


class VigiOnline(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, channel: int) -> None:
        super().__init__(coord)
        self.channel = channel
        self._attr_unique_id = f"{coord.entry.entry_id}_online_{channel}"
        self._attr_device_info = device_info(coord)

    @property
    def name(self) -> str:
        d = self.coordinator.channel(self.channel)
        return f"{d['name'] if d else f'Kanaal {self.channel}'} verbinding"

    @property
    def is_on(self) -> bool:
        d = self.coordinator.channel(self.channel)
        return bool(d and d["online"])
