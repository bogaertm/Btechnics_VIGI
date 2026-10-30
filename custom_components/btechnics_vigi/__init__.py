"""Btechnics VIGI: TP-Link VIGI NVR in Home Assistant (live beeld, opnames, status)."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VigiClient, VigiError
from .const import DOMAIN, SCAN_INTERVAL_S
from .replay import ReplayManager
from .websocket import async_register_ws

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CAMERA, Platform.BINARY_SENSOR]


class VigiCoordinator(DataUpdateCoordinator[list[dict]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: VigiClient) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {entry.data[CONF_HOST]}",
                         update_interval=timedelta(seconds=SCAN_INTERVAL_S), config_entry=entry)
        self.client = client
        self.entry = entry
        self.replay = ReplayManager(hass, client)

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self.client.devices()
        except VigiError as e:
            raise UpdateFailed(str(e)) from e

    def channel(self, ch: int) -> dict | None:
        return next((d for d in self.data or [] if d["channel"] == ch), None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass, verify_ssl=False)
    client = VigiClient(session, entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coord = VigiCoordinator(hass, entry, client)
    await coord.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    async_register_ws(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord: VigiCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.replay.stop_all()
    return ok
