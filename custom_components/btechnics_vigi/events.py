"""Meldingen van de recorder (beweging, persoon, voertuig, ...) via de event server van de VIGI OpenAPI.

Bron: VIGI NVR OpenAPI Document v1.2, hoofdstuk 2.1.1 en 3 (multipart POST met een JSON-deel), 4.8 (event_server
instellen, id 1 tot 3) en bijlage IV (type en sub_type). De recorder stuurt de meldingen zelf naar Home Assistant;
het adres bevat een geheime sleutel en enkel het IP van de recorder wordt aanvaard (de view vraagt geen login,
want de recorder kan niet aanmelden bij HA).

Bewaard: 14 dagen in .storage (voor de markeringen op de tijdlijn). Elke melding gaat ook als HA-event
"btechnics_vigi_event" op de bus (voor automatisaties) en zet een bewegingssensor per kanaal 30 s aan.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import time
from datetime import datetime
from urllib.parse import unquote

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import VigiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
EVENT_PATH = "/api/btechnics_vigi/event/"
BUS_EVENT = f"{DOMAIN}_event"
KEEP_S = 14 * 86400
MAX_EVENTS = 60000
HOLD_S = 30
SIGNAL = f"{DOMAIN}_motion_{{}}"

# bijlage IV: sub_type van type 1 (kanaal)
_SUB = {1: "tijd", 2: "beweging", 3: "sabotage", 4: "lijn", 5: "inbraak", 6: "gebied in", 7: "gebied uit",
        8: "rondhangen", 9: "menigte", 10: "snelle beweging", 11: "parkeren", 12: "voorwerp achtergelaten",
        13: "voorwerp weg", 14: "geluid", 15: "onscherp", 16: "beeld gewijzigd", 17: "gezicht", 18: "alarm",
        19: "beeldverlies", 20: "videobericht", 21: "persoon", 22: "voertuig", 23: "achtergelaten of weg",
        24: "deurbel", 25: "huilen", 26: "noodoproep", 36: "I-frame", 38: "alarmingang"}
_DEV = {1: "SD-kaart weg", 2: "SD-kaart vol", 3: "schijf weg", 4: "schijf vol", 5: "toestel offline",
        6: "SD-kaart fout", 7: "schijffout", 8: "aanmeldfout", 9: "PoE kortsluiting", 10: "PoE overbelast",
        11: "PoE temperatuur", 12: "PoE totaal overbelast", 13: "alarmingang", 14: "ventilator", 15: "IP-conflict",
        17: "schijf uitgeschakeld"}
_STORE = {1: "RAID offline", 2: "RAID gedegradeerd", 3: "RAID-schijf", 4: "RAID-reserve", 5: "geen array",
          6: "array vol", 7: "geen schijf", 8: "schijfwachtwoord", 9: "RAID-wachtwoord", 10: "wachtwoord uitbreiding"}
# welke meldingen de bewegingssensoren aanzetten
MOTION_KINDS = ("beweging", "persoon", "voertuig")


def classify(msg: dict) -> tuple[str, str]:
    """(soort, tekst). Soort is beweging, persoon, voertuig, of de naam uit bijlage IV."""
    t = _int(msg.get("type"))
    subs = [_int(x) for x in (msg.get("sub_type") if isinstance(msg.get("sub_type"), list) else [msg.get("sub_type")])]
    subs = [s for s in subs if s is not None]
    if t == 1:
        main = subs[0] if subs else None
        base = _SUB.get(main, f"melding {main}")
        if 101 in subs or main == 21:
            kind = "persoon"
        elif 102 in subs or main == 22:
            kind = "voertuig"
        elif main in (2, 10):
            kind = "beweging"
        else:
            kind = base
        text = f"{base}: {kind}" if kind in ("persoon", "voertuig") and base not in ("persoon", "voertuig") else base
        return kind, text
    if t == 2:
        return "toestel", _DEV.get(subs[0] if subs else None, "toestelfout")
    if t == 7:
        return "opslag", _STORE.get(subs[0] if subs else None, "opslagfout")
    return "andere", f"type {t}"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dec(v):
    if isinstance(v, str):
        return unquote(v)
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        return {k: _dec(x) for k, x in v.items()}
    return v


def parse_payload(body: bytes, content_type: str) -> dict | None:
    """JSON-deel uit de multipart halen (of gewone JSON). Beelden (JPEG-delen) worden genegeerd."""
    candidates = []
    if "multipart" in (content_type or ""):
        b = None
        for p in content_type.split(";"):
            p = p.strip()
            if p.lower().startswith("boundary="):
                b = p.split("=", 1)[1].strip('"')
        seps = [("--" + b).encode()] if b else []
        for sep in seps or [b"--"]:
            for part in body.split(sep):
                if b"{" in part:
                    candidates.append(part[part.index(b"{"):part.rindex(b"}") + 1] if b"}" in part else b"")
    if not candidates and b"{" in body:
        candidates.append(body[body.index(b"{"):body.rindex(b"}") + 1])
    for c in candidates:
        try:
            data = json.loads(c.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(data, dict):
            return _dec(data)
    return None


def _when(msg: dict) -> float:
    lt = msg.get("localtime")
    if lt:
        try:
            return float(lt) if str(lt).isdigit() else dt_util.as_timestamp(
                dt_util.parse_datetime(str(lt)) or datetime.strptime(str(lt), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt_util.get_default_time_zone()))
        except (ValueError, TypeError):
            pass
    return time.time()


class EventHub:
    def __init__(self, hass: HomeAssistant, coord) -> None:
        self.hass = hass
        self.coord = coord
        self.entry = coord.entry
        self._store = Store(hass, 1, f"{DOMAIN}.{coord.entry.entry_id}.events")
        self.events: list[list] = []          # [tijd, kanaal, soort, tekst]
        self.last: dict[tuple[int, str], float] = {}
        self.debug: list[dict] = []           # laatste ontvangen berichten (diagnose)
        self.received = 0
        self.status = "niet ingesteld"

    @property
    def token(self) -> str:
        return self.entry.data.get("event_token", "")

    async def async_load(self) -> None:
        if not self.token:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, "event_token": secrets.token_urlsafe(24)})
        data = await self._store.async_load() or {}
        cut = time.time() - KEEP_S
        self.events = [e for e in data.get("events", []) if e[0] >= cut]

    async def async_save_now(self) -> None:
        await self._store.async_save({"events": self.events})

    def _data(self) -> dict:
        cut = time.time() - KEEP_S
        if self.events and self.events[0][0] < cut:
            self.events = [e for e in self.events if e[0] >= cut]
        return {"events": self.events[-MAX_EVENTS:]}

    def allowed(self, remote: str | None) -> bool:
        """Enkel de recorder zelf (IP uit de koppeling). Is de host een naam, dan enkel een privé-adres."""
        host = self.coord.client.host
        try:
            ipaddress.ip_address(host)
            return remote == host
        except ValueError:
            try:
                return ipaddress.ip_address(remote or "").is_private
            except ValueError:
                return False

    @callback
    def handle(self, payload: dict, remote: str | None) -> int:
        self.received += 1
        self.debug = (self.debug + [{"t": time.time(), "from": remote, "data": payload}])[-5:]
        n = 0
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            kind, text = classify(msg)
            if kind == "tijd":   # tijdsgebonden opname, geen melding
                continue
            ch = _int(msg.get("channel")) or 0
            ts = _when(msg)
            self.events.append([round(ts, 1), ch, kind, text])
            name = msg.get("channel_name") or msg.get("channel_na") or ""
            d = self.coord.channel(ch) if ch else None
            name = (d or {}).get("name") or name or (f"Kanaal {ch}" if ch else "Recorder")
            self.hass.bus.async_fire(BUS_EVENT, {
                "entry_id": self.entry.entry_id, "channel": ch, "channel_name": name, "kind": kind, "text": text,
                "type": _int(msg.get("type")), "sub_type": msg.get("sub_type"), "time": dt_util.utc_from_timestamp(ts).isoformat()})
            if kind in MOTION_KINDS and ch:
                self.last[(ch, kind)] = time.time()
                if kind != "beweging":   # persoon en voertuig zijn ook beweging
                    self.last[(ch, "beweging")] = time.time()
                async_dispatcher_send(self.hass, SIGNAL.format(self.entry.entry_id))
            n += 1
        if n:
            self._store.async_delay_save(self._data, 30)
        return n

    def day(self, channel: int, start: float, end: float) -> list[list]:
        return [[e[0], e[2], e[3]] for e in self.events if e[1] == channel and start <= e[0] < end]

    # ---- event server op de recorder ----
    async def async_sync(self, enabled: bool) -> None:
        """Eigen event server op de recorder zetten (of weghalen). Andere servers blijven ongemoeid."""
        c = self.coord.client
        try:
            cur = (await c.request("GET", "/openapi/event_server")).get("event_server") or []
        except VigiError as e:
            self.status = f"lezen mislukt: {e}"
            _LOGGER.warning("Event server lezen mislukt: %s", e)
            return
        mine = [s for s in cur if str(s.get("url", "")).startswith(EVENT_PATH)]
        if not enabled:
            for s in mine:
                await self._delete(int(s["id"]))
            self.status = "uit"
            return
        ip, port, proto = await self._target()
        if not ip:
            self.status = "geen IP-adres van Home Assistant gevonden"
            return
        want = {"ip_or_domain": ip, "port": port, "url": EVENT_PATH + self.token, "protocol": proto,
                "picture_switch": "off"}
        ok = [s for s in mine if all(str(s.get(k)) == str(v) for k, v in want.items())]
        if ok:
            for s in mine:
                if s is not ok[0]:
                    await self._delete(int(s["id"]))
            self.status = f"actief (id {ok[0]['id']}, {ip}:{port})"
            return
        for s in mine:
            await self._delete(int(s["id"]))
        used = {int(s["id"]) for s in cur if s not in mine}
        free = next((i for i in (1, 2, 3) if i not in used), None)
        if free is None:
            self.status = "alle 3 plaatsen voor een event server zijn bezet op de recorder"
            _LOGGER.warning("Btechnics VIGI: %s", self.status)
            return
        err = None
        for path in ("/openapi/event_server/new_server", "/openapi/event_server"):
            try:
                await c.request("POST", path, json_body={"id": free, **want})
                err = None
                break
            except VigiError as e:
                err = e
        if err:
            self.status = f"instellen mislukt: {err}"
            _LOGGER.warning("Event server instellen mislukt: %s", err)
            return
        self.status = f"actief (id {free}, {ip}:{port})"
        _LOGGER.info("Btechnics VIGI: event server ingesteld op de recorder (id %s, %s:%s)", free, ip, port)

    async def _delete(self, sid: int) -> None:
        try:
            await self.coord.client.request("POST", "/openapi/event_server/delete_server", json_body={"id": sid})
        except VigiError as e:
            _LOGGER.warning("Event server %s weghalen mislukt: %s", sid, e)

    async def _target(self) -> tuple[str | None, int, str]:
        api = getattr(self.hass.config, "api", None)
        port = getattr(api, "port", None) or 8123
        proto = "HTTPS" if getattr(api, "use_ssl", False) else "HTTP"
        ip = None
        try:
            from homeassistant.components.network import async_get_source_ip  # noqa: PLC0415
            target = self.coord.client.host
            try:
                ipaddress.ip_address(target)
            except ValueError:
                target = None
            ip = await async_get_source_ip(self.hass, target_ip=target) if target else await async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001
            ip = getattr(api, "local_ip", None)
        return ip, int(port), proto
