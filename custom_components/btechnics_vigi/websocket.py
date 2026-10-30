"""WebSocket-commando's. Opnames enkel voor beheerders (keuze Matthias 30/10/2026, camerawet)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

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
    for f in (ws_channels, ws_days, ws_search, ws_replay, ws_replay_stop, ws_live, ws_webrtc, ws_ice, ws_clip_start, ws_clip_status,
              ws_probe, ws_replay_probe):
        websocket_api.async_register_command(hass, f)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/channels", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_channels(hass, connection, msg):
    c = _coord(hass, msg.get("entry_id"))
    if not c:
        connection.send_error(msg["id"], "not_found", "geen recorder")
        return
    admin = connection.user.is_admin
    # Audit 30/10/2026: gewone gebruikers kregen IP en MAC van recorder en camera's
    chans = [d if admin else {"channel": d["channel"], "name": d["name"], "online": d["online"]}
             for d in (c.data or [])]
    connection.send_result(msg["id"], {"entry_id": c.entry.entry_id, "host": c.client.host if admin else None,
                                       "channels": chans, "admin": admin})


def _admin_coord(hass, connection, msg):
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "enkel beheerders")
        return None
    c = _coord(hass, msg.get("entry_id"))
    if not c:
        connection.send_error(msg["id"], "not_found", "geen recorder")
    return c


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/record/days", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Optional("start"): vol.Match(r"^\d{6}$"),
                                  vol.Optional("end"): vol.Match(r"^\d{6}$")})
@websocket_api.async_response
async def ws_days(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    now = dt_util.now()   # tijdzone van HA (audit 30/10/2026: was systeemtijd)
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
    connection.send_result(msg["id"], {"channel": msg["channel"], "days": days, "start": start, "end": end})


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
        url = await c.replay.start(msg["channel"], msg["start"], msg.get("end"), connection.user.id)
    except Exception as e:  # noqa: BLE001  fout naar de kaart
        connection.send_error(msg["id"], "replay_error", str(e)[:200])
        return
    connection.send_result(msg["id"], {"url": url})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/replay/stop", vol.Optional("entry_id"): str,
                                  vol.Optional("url"): str})
@websocket_api.async_response
async def ws_replay_stop(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    connection.send_result(msg["id"], {"stopped": await c.replay.stop(connection.user.id, msg.get("url"))})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/live", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int})
@websocket_api.async_response
async def ws_live(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    if not c.channel(msg["channel"]):
        connection.send_error(msg["id"], "not_found", "onbekend kanaal")
        return
    try:
        url = await c.live.url(msg["channel"])
    except Exception as e:  # noqa: BLE001
        connection.send_error(msg["id"], "live_error", str(e)[:200])
        return
    connection.send_result(msg["id"], {"url": url})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/webrtc", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Required("offer"): str})
@websocket_api.async_response
async def ws_webrtc(hass, connection, msg):
    """Live via WebRTC: SDP-aanbod van de browser naar go2rtc, antwoord terug (zonder trickle)."""
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    if not c.channel(msg["channel"]):
        connection.send_error(msg["id"], "not_found", "onbekend kanaal")
        return
    try:
        answer = await c.live.webrtc_answer(msg["channel"], msg["offer"])
    except Exception as e:  # noqa: BLE001
        connection.send_error(msg["id"], "webrtc_error", str(e)[:200].replace(c.client.password_for_diagnose(), "***"))
        return
    ice = []
    try:
        from homeassistant.components.web_rtc import async_get_ice_servers  # noqa: PLC0415
        for s in async_get_ice_servers(hass):
            d = {"urls": s.urls}
            if getattr(s, "username", None):
                d.update(username=s.username, credential=s.credential)
            ice.append(d)
    except Exception:  # noqa: BLE001
        pass
    connection.send_result(msg["id"], {"answer": answer, "ice_servers": ice})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/ice", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_ice(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    ice = []
    try:
        from homeassistant.components.web_rtc import async_get_ice_servers  # noqa: PLC0415
        for s in async_get_ice_servers(hass):
            d = {"urls": s.urls}
            if getattr(s, "username", None):
                d.update(username=s.username, credential=s.credential)
            ice.append(d)
    except Exception:  # noqa: BLE001
        ice = [{"urls": ["stun:stun.home-assistant.io:3478"]}]
    connection.send_result(msg["id"], {"ice_servers": ice})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/clip/start", vol.Optional("entry_id"): str,
                                  vol.Required("channel"): int, vol.Required("start"): int, vol.Required("end"): int,
                                  vol.Optional("parts"): [[int]]})
@websocket_api.async_response
async def ws_clip_start(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    ch = c.channel(msg["channel"])
    parts = [p for p in (msg.get("parts") or []) if len(p) == 2 and p[1] > p[0]]
    try:
        clip = await c.clips.start(msg["channel"], ch["name"] if ch else f"Kanaal {msg['channel']}",
                                   msg["start"], msg["end"], parts, connection.user.name or connection.user.id)
    except Exception as e:  # noqa: BLE001
        connection.send_error(msg["id"], "clip_error", str(e)[:200])
        return
    connection.send_result(msg["id"], clip.status())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/clip/status", vol.Optional("entry_id"): str,
                                  vol.Required("clip_id"): str})
@websocket_api.async_response
async def ws_clip_status(hass, connection, msg):
    c = _admin_coord(hass, connection, msg)
    if not c:
        return
    clip = c.clips.clips.get(msg["clip_id"])
    if not clip:
        connection.send_error(msg["id"], "not_found", "opname niet (meer) beschikbaar")
        return
    connection.send_result(msg["id"], clip.status())


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
            out[t] = await rtsp_rate(url, c.client.username, c.client.password_for_diagnose(), heads[t], sec)
    connection.send_result(msg["id"], out)
