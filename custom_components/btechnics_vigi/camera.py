"""Een camera-entiteit per kanaal: live via de substream (H.264), beeld via /openapi/snapshot."""
from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import VigiError
from .const import DOMAIN, LIVE_STREAM


async def async_setup_entry(hass, entry, async_add_entities):
    coord = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(VigiCamera(coord, d["channel"]) for d in coord.data or [])


def device_info(coord) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, coord.entry.entry_id)}, name="VIGI recorder",
                      manufacturer="TP-Link", model="VIGI NVR", configuration_url=f"https://{coord.client.host}")


class VigiCamera(CoordinatorEntity, Camera):
    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coord, channel: int) -> None:
        CoordinatorEntity.__init__(self, coord)
        Camera.__init__(self)
        self.channel = channel
        self._attr_unique_id = f"{coord.entry.entry_id}_cam_{channel}"
        self._attr_device_info = device_info(coord)

    @property
    def name(self) -> str:
        d = self.coordinator.channel(self.channel)
        return d["name"] if d else f"Kanaal {self.channel}"

    @property
    def available(self) -> bool:
        d = self.coordinator.channel(self.channel)
        return bool(super().available and d and d["online"])

    @property
    def extra_state_attributes(self):
        d = self.coordinator.channel(self.channel) or {}
        return {"kanaal": self.channel, "ip": d.get("ip")}

    async def stream_source(self) -> str | None:
        return self.coordinator.client.live_url(self.channel, LIVE_STREAM)

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        try:
            return await self.coordinator.client.snapshot(self.channel)
        except VigiError:
            return None
