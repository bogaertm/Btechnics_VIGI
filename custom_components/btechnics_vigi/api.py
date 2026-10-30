"""Client voor de VIGI NVR OpenAPI (document v1.4, TP-Link, juli 2026).

Aanmelden: GET /openapi/token met Digest (SHA-256), daarna Bearer-token (30 min) met refresh token.
Alle tekstwaarden in JSON zijn percent-encoded (hoofdstuk 4, opmerking 2): na ontvangst decoderen.
Enkel HTTPS op poort 20443; het certificaat van de recorder is zelfondertekend.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote

import aiohttp

from .const import OPENAPI_PORT, REPLAY_STREAM, RTSP_PORT


class VigiError(Exception):
    """Fout van de recorder of de verbinding."""

    def __init__(self, msg: str, code: int | None = None) -> None:
        super().__init__(msg)
        self.code = code


class VigiAuthError(VigiError):
    """Gebruiker of wachtwoord geweigerd."""


def _decode(v: Any) -> Any:
    if isinstance(v, str):
        return unquote(v)
    if isinstance(v, list):
        return [_decode(x) for x in v]
    if isinstance(v, dict):
        return {k: _decode(x) for k, x in v.items()}
    return v


def parse_challenge(header: str) -> dict[str, str]:
    """WWW-Authenticate: Digest realm="..", nonce="..", algorithm="SHA-256", ..."""
    header = re.sub(r"^\s*Digest\s+", "", header, flags=re.I)
    return {k.lower(): (v1 if v1 != "" or v2 == "" else v2)
            for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', header)}


def digest_header(ch: dict[str, str], username: str, password: str, method: str, uri: str,
                  cnonce: str | None = None, nc: str = "00000001") -> str:
    """Digest-antwoord volgens RFC 7616; algoritme uit de uitdaging (standaard SHA-256 bij VIGI)."""
    alg = (ch.get("algorithm") or "SHA-256").upper()
    h = (lambda s: hashlib.md5(s.encode()).hexdigest()) if alg.startswith("MD5") else \
        (lambda s: hashlib.sha256(s.encode()).hexdigest())
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    ha1 = h(f"{username}:{realm}:{password}")
    ha2 = h(f"{method}:{uri}")
    qop = ch.get("qop")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if qop:
        qop = "auth" if "auth" in [q.strip() for q in qop.split(",")] else qop.split(",")[0].strip()
        cnonce = cnonce or os.urandom(8).hex()
        resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        resp = h(f"{ha1}:{nonce}:{ha2}")
    parts += [f'response="{resp}"', f'algorithm={ch.get("algorithm") or "SHA-256"}']
    if ch.get("opaque"):
        parts.append(f'opaque="{ch["opaque"]}"')
    return "Digest " + ", ".join(parts)


def utc_stamp(ts: float) -> str:
    """Epoch naar YYYYMMDDtHHMMSSz (UTC), zoals de replay-URL vraagt."""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y%m%dt%H%M%Sz")


class VigiClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, username: str, password: str,
                 port: int = OPENAPI_PORT, rtsp_port: int = RTSP_PORT, scheme: str = "https") -> None:
        self._s = session
        self.scheme = scheme
        self.host, self.username, self._password = host, username, password
        self.port, self.rtsp_port = port, rtsp_port
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self._search_lock = asyncio.Lock()
        self._proc: int | None = None
        self._gen = 0

    @property
    def base(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    # ---------- aanmelden ----------
    # Audit 30/10/2026: netwerkfouten werden geen VigiError (ClientConnectorError ontsnapte), een mislukte refresh
    # viel niet terug op Digest, aanmelden had geen eigen timeout en gelijktijdige 401's gaven elk een nieuwe login.
    _TOKEN_TIMEOUT = aiohttp.ClientTimeout(total=15)

    async def _token_digest(self) -> None:
        uri = "/openapi/token"
        async with self._s.get(self.base + uri, ssl=False, timeout=self._TOKEN_TIMEOUT) as r:
            if r.status != 401:
                raise VigiError(f"onverwacht antwoord bij aanmelden: HTTP {r.status}")
            ch = parse_challenge(r.headers.get("WWW-Authenticate", ""))
        auth = digest_header(ch, self.username, self._password, "GET", uri)
        async with self._s.get(self.base + uri, headers={"Authorization": auth}, ssl=False,
                               timeout=self._TOKEN_TIMEOUT) as r:
            if r.status == 401:
                raise VigiAuthError("gebruiker of wachtwoord geweigerd")
            if r.status != 200:
                raise VigiError(f"aanmelden mislukt: HTTP {r.status}")
            data = await r.json(content_type=None)
        if not data.get("access_token"):
            code = data.get("error_code")
            if code in (-40401, -70302, -70312, -70301):
                raise VigiAuthError(f"aanmelden geweigerd: foutcode {code}")
            raise VigiError(f"geen token ontvangen: foutcode {code}", code)
        self._store(data)

    async def _token_refresh(self) -> bool:
        if not self._refresh:
            return False
        try:
            async with self._s.get(self.base + "/openapi/token", ssl=False, timeout=self._TOKEN_TIMEOUT,
                                   params={"grant_type": "refresh_token", "refresh_token": self._refresh}) as r:
                if r.status != 200:
                    raise ValueError(r.status)
                data = await r.json(content_type=None)
            if not data.get("access_token"):
                raise ValueError(data.get("error_code"))
            self._store(data)
            return True
        except Exception:  # noqa: BLE001  elke mislukte refresh: refresh token weg, terug naar Digest
            self._refresh = None
            return False

    def _store(self, data: dict) -> None:
        self._access = unquote(data["access_token"])
        self._refresh = unquote(data.get("refresh_token") or "") or None
        self._expires = time.monotonic() + int(data.get("expires_in") or 1800) - 60
        self._gen += 1

    async def _ensure_token(self, force_gen: int | None = None) -> None:
        """force_gen: het token van die generatie werd geweigerd; enkel vernieuwen als niemand dat al deed."""
        async with self._lock:
            if force_gen is None and self._access and time.monotonic() < self._expires:
                return
            if force_gen is not None and self._gen != force_gen and self._access:
                return
            try:
                if not await self._token_refresh():
                    await self._token_digest()
            except VigiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                raise VigiError(f"aanmelden: {e.__class__.__name__} {e}") from e

    # ---------- aanvragen ----------
    async def request(self, method: str, path: str, params: dict | None = None,
                      json_body: dict | None = None, raw: bool = False) -> Any:
        await self._ensure_token()
        for poging in (1, 2):
            gen = self._gen
            headers = {"Authorization": f"Bearer {self._access}"}
            body = None
            if json_body is not None:
                body = {k: quote(v, safe="") if isinstance(v, str) else v for k, v in json_body.items()}
            try:
                async with self._s.request(method, self.base + path, params=params, json=body,
                                           headers=headers, ssl=False,
                                           timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 401 and poging == 1:
                        retry = True
                    else:
                        retry = False
                        if r.status != 200:
                            raise VigiError(f"{path}: HTTP {r.status}")
                        if raw:
                            return await r.read()
                        data = _decode(await r.json(content_type=None))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                raise VigiError(f"{path}: {e.__class__.__name__} {e}") from e
            if retry:
                await self._ensure_token(force_gen=gen)
                continue
            if isinstance(data, dict) and data.get("error_code", 0) != 0:
                raise VigiError(f"{path}: foutcode {data.get('error_code')}", data.get("error_code"))
            return data
        raise VigiError(f"{path}: geweigerd na nieuw token")

    # ---------- lezen ----------
    async def modules(self) -> list[dict]:
        return (await self.request("GET", "/openapi/module_list")).get("module_list", [])

    async def devices(self) -> list[dict]:
        out = []
        for d in (await self.request("GET", "/openapi/added_devices")).get("devices", []):
            out.append({"channel": int(d.get("id")), "name": d.get("name") or f"Kanaal {d.get('id')}",
                        "alias": d.get("alias"), "online": str(d.get("online")) == "1",
                        "ip": d.get("ip"), "mac": d.get("mac")})
        return sorted(out, key=lambda d: d["channel"])

    async def snapshot(self, channel: int) -> bytes:
        return await self.request("GET", "/openapi/snapshot", params={"channel": channel}, raw=True)

    async def record_days(self, channel: int, start: str, end: str) -> list[str]:
        """Dagen met opnames (YYYYMMDD) tussen maand start en end (YYYYMM)."""
        d = await self.request("GET", "/openapi/record/days",
                               params={"channel": channel, "start": start, "end": end})
        return [x["day"] for x in d.get("days", []) if x.get("day")]

    # Zoekprocessen: de recorder kent er maximaal 4 per keer (foutcode -71101, API-document bijlage I).
    # Daarom een proces aanvragen en hergebruiken, en zoekopdrachten na elkaar uitvoeren.
    BUSY = (-71115, -40109)          # zoekt nog / systeem bezig: even wachten
    FULL = -71101                    # geen vrij zoekproces
    STALE = (-71103, -71102)         # proces-id ongeldig of bezet: nieuw aanvragen

    async def _free_process(self) -> int:
        for poging in range(4):
            try:
                proc = (await self.request("GET", "/openapi/record/search/free_process")).get("process")
                if proc is None:
                    raise VigiError("geen vrij zoekproces")
                return int(proc)
            except VigiError as e:
                if e.code != self.FULL or poging == 3:
                    raise
                await asyncio.sleep(2 + 2 * poging)
        raise VigiError("geen vrij zoekproces")

    async def _results(self, params: dict) -> dict:
        for poging in range(8):
            try:
                return await self.request("GET", "/openapi/record/search/results", params=params)
            except VigiError as e:
                if e.code in self.BUSY and poging < 7:
                    await asyncio.sleep(1)
                    continue
                raise
        raise VigiError("zoeken duurt te lang")

    async def record_search(self, channel: int, day: str, page: int = 100, limit: int = 5000) -> list[dict]:
        """Opnamestukken van een dag (YYYYMMDD) als {start, end} epoch, opeenvolgende stukken samengevoegd."""
        async with self._search_lock:
            for herstart in (1, 2):
                if self._proc is None:
                    self._proc = await self._free_process()
                try:
                    return merge_segments(await self._search(self._proc, channel, day, page, limit))
                except VigiError as e:
                    if e.code in self.STALE and herstart == 1:
                        self._proc = None
                        continue
                    raise
        return []

    async def _search(self, proc: int, channel: int, day: str, page: int, limit: int) -> list[dict]:
        res: list[dict] = []
        start = 0
        while start < limit:
            try:
                d = await self._results({"channel": channel, "process": proc, "day": day,
                                         "start_index": start, "end_index": start + page - 1})
            except VigiError as e:
                # Audit 30/10/2026: andere fouten na pagina 1 gaven stil een half resultaat
                if e.code in (-71104, -40205):  # geen (verdere) resultaten
                    return res
                raise
            batch = d.get("results") or []
            res += [{"start": int(x["start_time"]), "end": int(x["end_time"])} for x in batch]
            if len(batch) < page:
                break
            start += page
        return res

    def password_for_diagnose(self) -> str:
        return self._password

    def rtsp_url(self, path: str) -> str:
        cred = f"{quote(self.username, safe='')}:{quote(self._password, safe='')}@"
        return f"rtsp://{cred}{self.host}:{self.rtsp_port}{path}"

    def live_url(self, channel: int, stream: int) -> str:
        return self.rtsp_url(f"/live/{channel}/{stream}/avm")

    def replay_url(self, channel: int, start: float, end: float, stream: int = REPLAY_STREAM) -> str:
        return self.rtsp_url(f"/replay/{channel}/{stream}/avm?starttime={utc_stamp(start)}&endtime={utc_stamp(end)}")


def merge_segments(segs: list[dict], gap: int = 20) -> list[dict]:
    """Stukken met tot 20 s ertussen samenvoegen; HA verdraagt een tijdsprong tot 30 s in een stream (MAX_TIMESTAMP_GAP).
    Elk samengevoegd stuk houdt zijn doorlopende deelstukken bij in "parts" (nodig om exact binnen te halen)."""
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["start"]):
        if out and s["start"] - out[-1]["end"] <= gap:
            last = out[-1]
            if s["start"] <= last["parts"][-1][1]:
                last["parts"][-1][1] = max(last["parts"][-1][1], s["end"])
            else:
                last["parts"].append([s["start"], s["end"]])
            last["end"] = max(last["end"], s["end"])
        else:
            out.append({"start": s["start"], "end": s["end"], "parts": [[s["start"], s["end"]]]})
    return out
