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

    @property
    def base(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    # ---------- aanmelden ----------
    async def _token_digest(self) -> None:
        uri = "/openapi/token"
        async with self._s.get(self.base + uri, ssl=False) as r:
            if r.status != 401:
                raise VigiError(f"onverwacht antwoord bij aanmelden: HTTP {r.status}")
            ch = parse_challenge(r.headers.get("WWW-Authenticate", ""))
        auth = digest_header(ch, self.username, self._password, "GET", uri)
        async with self._s.get(self.base + uri, headers={"Authorization": auth}, ssl=False) as r:
            if r.status == 401:
                raise VigiAuthError("gebruiker of wachtwoord geweigerd")
            if r.status != 200:
                raise VigiError(f"aanmelden mislukt: HTTP {r.status}")
            self._store(await r.json(content_type=None))

    async def _token_refresh(self) -> bool:
        if not self._refresh:
            return False
        try:
            async with self._s.get(self.base + "/openapi/token", ssl=False,
                                   params={"grant_type": "refresh_token", "refresh_token": self._refresh}) as r:
                if r.status != 200:
                    return False
                self._store(await r.json(content_type=None))
                return True
        except aiohttp.ClientError:
            return False

    def _store(self, data: dict) -> None:
        if not data.get("access_token"):
            raise VigiError(f"geen token ontvangen: {data.get('error_code')}")
        self._access = unquote(data["access_token"])
        self._refresh = unquote(data.get("refresh_token") or "") or None
        self._expires = time.monotonic() + int(data.get("expires_in") or 1800) - 60

    async def _ensure_token(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._access and time.monotonic() < self._expires:
                return
            if not await self._token_refresh():
                await self._token_digest()

    # ---------- aanvragen ----------
    async def request(self, method: str, path: str, params: dict | None = None,
                      json_body: dict | None = None, raw: bool = False) -> Any:
        await self._ensure_token()
        for poging in (1, 2):
            headers = {"Authorization": f"Bearer {self._access}"}
            body = None
            if json_body is not None:
                body = {k: quote(v, safe="") if isinstance(v, str) else v for k, v in json_body.items()}
            try:
                async with self._s.request(method, self.base + path, params=params, json=body,
                                           headers=headers, ssl=False,
                                           timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 401 and poging == 1:
                        await self._ensure_token(force=True)
                        continue
                    if r.status != 200:
                        raise VigiError(f"{path}: HTTP {r.status}")
                    if raw:
                        return await r.read()
                    data = _decode(await r.json(content_type=None))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise VigiError(f"{path}: {e.__class__.__name__} {e}") from e
            if isinstance(data, dict) and data.get("error_code", 0) != 0:
                raise VigiError(f"{path}: foutcode {data.get('error_code')}")
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

    async def record_search(self, channel: int, day: str, page: int = 100, limit: int = 5000) -> list[dict]:
        """Opnamestukken van een dag (YYYYMMDD) als {start, end} epoch, opeenvolgende stukken samengevoegd."""
        proc = (await self.request("GET", "/openapi/record/search/free_process")).get("process")
        if proc is None:
            raise VigiError("geen vrij zoekproces")
        res: list[dict] = []
        start = 0
        while start < limit:
            try:
                d = await self.request("GET", "/openapi/record/search/results", params={
                    "channel": channel, "process": proc, "day": day,
                    "start_index": start, "end_index": start + page - 1})
            except VigiError:
                if start == 0:
                    raise
                break  # start_index voorbij het einde
            batch = d.get("results") or []
            res += [{"start": int(x["start_time"]), "end": int(x["end_time"])} for x in batch]
            if len(batch) < page:
                break
            start += page
        return merge_segments(res)

    def rtsp_url(self, path: str) -> str:
        cred = f"{quote(self.username, safe='')}:{quote(self._password, safe='')}@"
        return f"rtsp://{cred}{self.host}:{self.rtsp_port}{path}"

    def live_url(self, channel: int, stream: int) -> str:
        return self.rtsp_url(f"/live/{channel}/{stream}/avm")

    def replay_url(self, channel: int, start: float, end: float, stream: int = REPLAY_STREAM) -> str:
        return self.rtsp_url(f"/replay/{channel}/{stream}/avm?starttime={utc_stamp(start)}&endtime={utc_stamp(end)}")


def merge_segments(segs: list[dict], gap: int = 2) -> list[dict]:
    """Aansluitende stukken (tot 2 s ertussen) samenvoegen; de recorder geeft kleine eenheden."""
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["start"]):
        if out and s["start"] - out[-1]["end"] <= gap:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out
