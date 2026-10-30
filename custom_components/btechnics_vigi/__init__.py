"""Btechnics VIGI: TP-Link VIGI NVR in Home Assistant (live beeld, opnames, status)."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VigiAuthError, VigiClient, VigiError
from .clips import ClipManager
from .const import CONF_CAMERA_ENTITIES, DOMAIN, SCAN_INTERVAL_S
from .replay import LiveManager, ReplayManager
from .views import SnapshotCache, async_register_views
from .websocket import async_register_ws

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CAMERA, Platform.BINARY_SENSOR]
CARDS_URL = "/btechnics_vigi_static"
CARDS_FILE = "btechnics-vigi-card.js"
_CARDS_KEY = f"{DOMAIN}_cards"


async def _async_register_cards(hass: HomeAssistant) -> None:
    """Dashboardkaart automatisch beschikbaar maken (zelfde werkwijze als Btechnics VTO)."""
    if hass.data.get(_CARDS_KEY) or getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        return
    hass.data[_CARDS_KEY] = True
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARDS_URL, str(Path(__file__).parent / "frontend"), False)])
    add_extra_js_url(hass, f"{CARDS_URL}/{CARDS_FILE}?v={version}")

    async def _at_start(_hass):
        await _async_ensure_resource(hass, version)

    async_at_started(hass, _at_start)


async def _async_ensure_resource(hass: HomeAssistant, version: str) -> None:
    try:
        resources = getattr(hass.data.get("lovelace"), "resources", None)
        if resources is None or not hasattr(resources, "async_create_item"):
            return
        if not getattr(resources, "loaded", False):
            await resources.async_load()
            resources.loaded = True
        base = f"{CARDS_URL}/{CARDS_FILE}"
        url = f"{base}?v={version}"
        mine = [r for r in resources.async_items() if str(r.get("url", "")).split("?")[0] == base]
        if not mine:
            await resources.async_create_item({"res_type": "module", "url": url})
        elif mine[0].get("url") != url:
            await resources.async_update_item(mine[0]["id"], {"res_type": "module", "url": url})
        for extra in mine[1:]:
            await resources.async_delete_item(extra["id"])
    except Exception:  # noqa: BLE001  de kaart komt dan nog via de HTML
        _LOGGER.exception("Dashboardresource registreren mislukt")


class VigiCoordinator(DataUpdateCoordinator[list[dict]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: VigiClient) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {entry.data[CONF_HOST]}",
                         update_interval=timedelta(seconds=SCAN_INTERVAL_S), config_entry=entry)
        self.client = client
        self.entry = entry
        self.replay = ReplayManager(hass, client)
        self.clips = ClipManager(hass, client, Path(hass.config.path("btechnics_vigi_clips", entry.entry_id)),
                                 streams=lambda: self.replay.count)
        self.snapshots = SnapshotCache(client)
        self.live = LiveManager(hass, client)

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self.client.devices()
        except VigiAuthError as e:
            # Audit 30/10/2026: na een wachtwoordwijziging bleef HA elke minuut aanmelden (risico: account geblokkeerd)
            raise ConfigEntryAuthFailed(str(e)) from e
        except VigiError as e:
            raise UpdateFailed(str(e)) from e

    def channel(self, ch: int) -> dict | None:
        return next((d for d in self.data or [] if d["channel"] == ch), None)


def _drop_old_clips(base: Path) -> None:
    """Tot v0.3.8 stonden de bestanden rechtstreeks in de map; nu per koppeling in een submap."""
    import shutil  # noqa: PLC0415
    if not base.exists():
        return
    for f in base.iterdir():
        if f.suffix == ".mp4" and f.is_file():
            f.unlink(missing_ok=True)
        elif f.suffix == ".tmp" and f.is_dir():
            shutil.rmtree(f, True)


def _platforms(entry: ConfigEntry) -> list[Platform]:
    # Camera-entiteiten enkel als gekozen (Audit 30/10/2026: camera.record en camera.snapshot zijn ook voor
    # gewone gebruikers bruikbaar en kunnen beelden naar /config/www schrijven, publiek via /local).
    return PLATFORMS if entry.options.get(CONF_CAMERA_ENTITIES, False) else [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass, verify_ssl=False)
    client = VigiClient(session, entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coord = VigiCoordinator(hass, entry, client)
    await coord.async_config_entry_first_refresh()
    await hass.async_add_executor_job(_drop_old_clips, Path(hass.config.path("btechnics_vigi_clips")))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    async_register_ws(hass)
    async_register_views(hass)
    await _async_register_cards(hass)
    if not entry.options.get(CONF_CAMERA_ENTITIES, False):
        reg = er.async_get(hass)
        for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
            if ent.domain == "camera":
                reg.async_remove(ent.entity_id)
    # bijhouden wat geladen is: bij unload na een optiewijziging zijn de opties al nieuw (review 30/10/2026)
    coord.platforms = _platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, coord.platforms)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord0 = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    ok = await hass.config_entries.async_unload_platforms(entry, getattr(coord0, "platforms", None) or _platforms(entry))
    if ok:
        coord: VigiCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.replay.stop_all()
        await coord.live.stop_all()
        await coord.clips.stop_all()
    return ok
