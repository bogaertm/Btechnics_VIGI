"""HTTP-adressen voor de kaart, enkel voor beheerders (camerawet, keuze Matthias 30/10/2026).

- /api/btechnics_vigi/snapshot/{entry_id}/{channel}: beeld van nu, gedeeld door alle kijkers (2,5 s bewaard),
  zodat 3 kijkers niet 3 keer de recorder belasten (audit 30/10/2026).
- /api/btechnics_vigi/clip/{clip_id}: binnengehaalde opname als mp4, met Range (spoelen). ?download=1 geeft
  een bestand en een regel in het logboek van HA (wie, welke camera, welk tijdstip).
De kaart gebruikt fetchWithAuth (beelden) of auth/sign_path (video en download).
"""
from __future__ import annotations

import asyncio
import time
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .api import VigiClient, VigiError
from .clips import clip_filename, file_exists
from .const import DOMAIN

_KEY = f"{DOMAIN}_views"
SNAP_TTL = 2.5


class SnapshotCache:
    def __init__(self, client: VigiClient) -> None:
        self.client = client
        self._data: dict[int, tuple[float, bytes]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, channel: int) -> bytes:
        lock = self._locks.setdefault(channel, asyncio.Lock())
        async with lock:
            hit = self._data.get(channel)
            if hit and time.monotonic() - hit[0] < SNAP_TTL:
                return hit[1]
            img = await self.client.snapshot(channel)
            self._data[channel] = (time.monotonic(), img)
            return img


def _coords(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {})


def _admin(request) -> bool:
    user = request.get("hass_user")
    return bool(user and user.is_admin)


class SnapshotView(HomeAssistantView):
    url = "/api/btechnics_vigi/snapshot/{entry_id}/{channel}"
    name = "api:btechnics_vigi:snapshot"
    requires_auth = True

    async def get(self, request, entry_id: str, channel: str):
        if not _admin(request):
            return web.Response(status=HTTPStatus.FORBIDDEN)
        coord = _coords(request.app["hass"]).get(entry_id)
        if not coord or not channel.isdigit():
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            img = await coord.snapshots.get(int(channel))
        except VigiError:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        return web.Response(body=img, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


class ClipView(HomeAssistantView):
    url = "/api/btechnics_vigi/clip/{clip_id}"
    name = "api:btechnics_vigi:clip"
    requires_auth = True

    async def get(self, request, clip_id: str):
        if not _admin(request):
            return web.Response(status=HTTPStatus.FORBIDDEN)
        hass = request.app["hass"]
        clip = None
        for coord in _coords(hass).values():
            clip = coord.clips.clips.get(clip_id)
            if clip:
                break
        if not clip or clip.state != "ready" or not await hass.async_add_executor_job(file_exists, clip.path):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        headers = {"Cache-Control": "private, max-age=3600"}
        if request.query.get("download"):
            fname = clip_filename(clip, dt_util.get_default_time_zone())
            headers["Content-Disposition"] = f'attachment; filename="{fname}"'
            _log_download(hass, request["hass_user"], clip)
        return web.FileResponse(clip.path, headers=headers)


def _log_download(hass: HomeAssistant, user, clip) -> None:
    """Spoor van elke kopie van beelden (camerawet)."""
    tz = dt_util.get_default_time_zone()
    van = dt_util.as_local(dt_util.utc_from_timestamp(clip.start)).astimezone(tz)
    tot = dt_util.as_local(dt_util.utc_from_timestamp(clip.end)).astimezone(tz)
    msg = (f"opname gedownload door {user.name}: {clip.channel_name} {van:%d/%m/%Y %H:%M:%S} tot {tot:%H:%M:%S}")
    try:
        from homeassistant.components.logbook import async_log_entry  # noqa: PLC0415
        async_log_entry(hass, "Btechnics VIGI", msg, DOMAIN)
    except Exception:  # noqa: BLE001  logboek niet geladen: dan enkel in het HA-log
        pass
    import logging  # noqa: PLC0415
    logging.getLogger(__name__).warning("Btechnics VIGI: %s", msg)


@callback
def async_register_views(hass: HomeAssistant) -> None:
    if hass.data.get(_KEY) or getattr(hass, "http", None) is None:
        return
    hass.data[_KEY] = True
    hass.http.register_view(SnapshotView())
    hass.http.register_view(ClipView())
