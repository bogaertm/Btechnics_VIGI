"""WebSocket-commando's. Opnames enkel voor beheerders (keuze Matthias 30/10/2026, camerawet)."""
from __future__ import annotations

from datetime import datetime

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .api import VigiError
from .const import DOMAIN

_REG = f"{DOMAIN}_ws_registered"


def _coords(hass: HomeAssistant) -> list:
    return list(hass.data.get(DOMAIN, {}).values())


def _coord(hass: HomeAssistant, entry_id: str | None):
    cs = _coords(hass)
    if entry_id:
        return next((c for c in cs if c.entry.entry_id == entry_id), None)
    return cs[0] if cs else None


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    if hass.data.get(_REG):
        return
    hass.data[_REG] = True
    for f in (ws_channels, ws_days, ws_search, ws_replay, ws_probe, ws_replay_probe):
        websocket_api.async_register_command(hass, f)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/channels", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_channels(hass, connection, msg):
    c = _coord(hass, msg.get("entry_id"))
    if not c:
        connection.send_error(msg["id"], "not_found", "geen recorder")
        return
    connection.send_result(msg["id"], {"entry_id": c.entry.entry_id, "host": c.client.host,
                                       "channels": c.data or [], "admin": connection.user.is_admin})


def _admin_coord(hass, connection, msg):
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "enkel beheerders")
        return None
    c = _coord(hass, msg.get("entry_id"))
    if not c:
        connection.send_error(msg["id"], "not_found", "geen recorder")
    return c


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/record/days", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Optional("start"): str, vol.Optional("end"): str})
@websocket_api.async_response
async def ws_days(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    now = datetime.now()
    end = msg.get("end") or now.strftime("%Y%m")
    y, m = now.year, now.month - 2  # standaard: deze maand en de 2 vorige
    if m <= 0:
        y, m = y - 1, m + 12
    start = msg.get("start") or f"{y}{m:02d}"
    try:
        days = await c.client.record_days(msg["channel"], start, end)
    except VigiError as e:
        connection.send_error(msg["id"], "vigi_error", str(e))
        return
    connection.send_result(msg["id"], {"channel": msg["channel"], "days": days})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/record/search", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Required("day"): vol.Match(r"^\d{8}$")})
@websocket_api.async_response
async def ws_search(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    try:
        segs = await c.client.record_search(msg["channel"], msg["day"])
    except VigiError as e:
        connection.send_error(msg["id"], "vigi_error", str(e))
        return
    connection.send_result(msg["id"], {"channel": msg["channel"], "day": msg["day"], "segments": segs})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/replay", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Required("start"): int, vol.Optional("end"): int})
@websocket_api.async_response
async def ws_replay(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    try:
        url = await c.replay.start(msg["channel"], msg["start"], msg.get("end"))
    except Exception as e:  # noqa: BLE001  fout naar de kaart sturen
        connection.send_error(msg["id"], "replay_error", str(e)[:200])
        return
    connection.send_result(msg["id"], {"url": url})


READ_ONLY = ("/openapi/module_list", "/openapi/added_devices", "/openapi/disks", "/openapi/event_server",
             "/openapi/record/days", "/openapi/resolution", "/openapi/bitrate", "/openapi/timing_mode",
             "/openapi/ntp", "/openapi/audio/capability")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/probe", vol.Optional("entry_id"): str,
                                  vol.Required("path"): vol.In(READ_ONLY), vol.Optional("params"): dict})
@websocket_api.async_response
async def ws_probe(hass, connection, msg):
    """Diagnose: enkel GET op een vaste lijst leesadressen."""
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    try:
        res = await c.client.request("GET", msg["path"], params=msg.get("params"))
    except VigiError as e:
        connection.send_error(msg["id"], "vigi_error", str(e))
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/replay_probe", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Required("start"): int, vol.Required("end"): int,
                                  vol.Optional("tests", default=["ffmpeg", "rtsp", "scale2", "scale4", "scale8", "speed4"]): [str],
                                  vol.Optional("seconds", default=8): vol.All(int, vol.Range(min=3, max=30))})
@websocket_api.async_response
async def ws_replay_probe(hass, connection, msg):
    """Diagnose: meet hoe snel de recorder een opname levert (enkel lezen)."""
    from .probe import ffmpeg_speed, rtsp_rate  # noqa: PLC0415

    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    url = c.client.replay_url(msg["channel"], msg["start"], msg["end"])
    sec = msg["seconds"]
    heads = {"rtsp": None, "scale2": {"Scale": "2.0"}, "scale4": {"Scale": "4.0"}, "scale8": {"Scale": "8.0"},
             "speed4": {"Speed": "4.0"}, "speed8": {"Speed": "8.0"}, "ratecontrol": {"Rate-Control": "no"}}
    out = {}
    for t in msg["tests"]:
        if t == "ffmpeg":
            try:
                from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415
                binary = get_ffmpeg_manager(hass).binary
            except Exception:  # noqa: BLE001
                binary = "ffmpeg"
            out[t] = await ffmpeg_speed(binary, url, sec + 4)
        elif t in heads:
            out[t] = await rtsp_rate(url, c.client.username, c.client._password, heads[t], sec)
    connection.send_result(msg["id"], out)
