"""Streams via de stream-integratie van HA: direct kijken naar een opname (replay) en live beeld.

Replay: de recorder speelt op echte snelheid (gemeten 30/10/2026). Dit dient enkel om meteen beeld te hebben;
spoelen, versnellen en downloaden gebeurt met het binnengehaalde bestand (clips.py).
use_wallclock_as_timestamps: de recorder springt in een replay over gaten in de opname, waardoor HA de stream
stopte ("Timestamp discontinuity detected", 30/10/2026: dts 55 s naar 393 s in een stuk van 27 s).
Max 4 replays tegelijk op de recorder: hier 1 per gebruiker en 2 in totaal; de rest gaat naar het binnenhalen.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.components.stream import HLS_PROVIDER, Stream, create_stream
from homeassistant.core import HomeAssistant

from .api import VigiClient
from .const import LIVE_STREAM, MAX_REPLAY_S

_LOGGER = logging.getLogger(__name__)
MAX_ACTIVE = 2


async def _stop(s: Stream, forget: bool = True) -> None:
    try:
        await s.stop()
    except Exception:  # noqa: BLE001  opruimen mag nooit falen
        _LOGGER.debug("stream stoppen mislukt", exc_info=True)
    if forget:
        # create_stream zet elke stream in hass.data["stream"]["streams"] en stop() haalt hem er niet uit: zonder dit
        # bleef per klik een Stream (met RTSP-adres) achter (review 30/10/2026). Interne lijst van HA, dus voorzichtig.
        try:
            from homeassistant.components.stream.const import ATTR_STREAMS, DOMAIN as STREAM_DOMAIN  # noqa: PLC0415
            lst = s.hass.data.get(STREAM_DOMAIN, {}).get(ATTR_STREAMS)
            if isinstance(lst, list) and s in lst:
                lst.remove(s)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("stream uit de lijst halen mislukt", exc_info=True)


class ReplayManager:
    def __init__(self, hass: HomeAssistant, client: VigiClient) -> None:
        self.hass = hass
        self.client = client
        self._active: list[tuple[str, str, Stream]] = []   # (gebruiker, url, stream)

    async def start(self, channel: int, start: int, end: int | None, user: str) -> str:
        end = end or start + 3600
        end = min(end, start + MAX_REPLAY_S)
        if end <= start:
            raise ValueError("einde moet na het begin liggen")
        # zelfde gebruiker: vorige replay eerst stoppen (een kijker heeft maar een speler)
        for item in [x for x in self._active if x[0] == user]:
            self._active.remove(item)
            await _stop(item[2])
        url = self.client.replay_url(channel, start, end)
        stream = create_stream(self.hass, url, {"rtsp_transport": "tcp", "use_wallclock_as_timestamps": True},
                               DynamicStreamSettings(), stream_label=f"VIGI replay kanaal {channel}")
        stream.add_provider(HLS_PROVIDER)
        await stream.start()
        hls = stream.endpoint_url(HLS_PROVIDER)
        self._active.append((user, hls, stream))
        while len(self._active) > MAX_ACTIVE:
            await _stop(self._active.pop(0)[2])
        return hls

    async def stop(self, user: str, url: str | None = None) -> bool:
        hit = [x for x in self._active if x[0] == user and (url is None or x[1] == url)]
        for item in hit:
            self._active.remove(item)
            await _stop(item[2])
        return bool(hit)

    @property
    def count(self) -> int:
        return len(self._active)

    async def stop_all(self) -> None:
        while self._active:
            await _stop(self._active.pop()[2])


class LiveManager:
    """Live beeld per kanaal (substream H.264), gedeeld door alle kijkers; HA stopt het na 30 s zonder kijker."""

    def __init__(self, hass: HomeAssistant, client: VigiClient) -> None:
        self.hass = hass
        self.client = client
        self._streams: dict[int, Stream] = {}

    async def url(self, channel: int) -> str:
        s = self._streams.get(channel)
        if s is None:
            s = create_stream(self.hass, self.client.live_url(channel, LIVE_STREAM), {"rtsp_transport": "tcp"},
                              DynamicStreamSettings(), stream_label=f"VIGI live kanaal {channel}")
            self._streams[channel] = s
        if HLS_PROVIDER not in s.outputs():
            s.add_provider(HLS_PROVIDER)
        await s.start()
        return s.endpoint_url(HLS_PROVIDER)

    async def stop_all(self) -> None:
        for s in self._streams.values():
            await _stop(s)
        self._streams.clear()

    # ---- WebRTC via go2rtc (ingebouwd in HA sinds 2024.11) ----
    # Live via HLS start pas na 2 segmenten van ca. 6 s (gemeten 30/10/2026: meer dan 12 s). Via go2rtc en WebRTC
    # is het beeld er meteen, zonder camera-entiteit (die gaven gewone gebruikers camera.record).
    def _go2rtc(self):
        from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
        cfg = self.hass.data.get("go2rtc")
        url = getattr(cfg, "url", None) or "http://localhost:11984/"
        session = getattr(cfg, "session", None) or async_get_clientsession(self.hass)
        return url.rstrip("/") + "/", session

    async def webrtc_answer(self, channel: int, offer_sdp: str) -> str:
        import aiohttp  # noqa: PLC0415
        if "go2rtc" not in self.hass.config.components:
            raise RuntimeError("go2rtc is niet actief in Home Assistant")
        base, session = self._go2rtc()
        name = f"btechnics_vigi_{self.client.host.replace('.', '_')}_{channel}"
        src = self.client.live_url(channel, LIVE_STREAM)
        to = aiohttp.ClientTimeout(total=15)
        async with session.put(base + "api/streams", params={"name": name, "src": src}, timeout=to) as r:
            if r.status >= 400:
                raise RuntimeError(f"go2rtc stream aanmaken: HTTP {r.status}")
        async with session.post(base + "api/webrtc", params={"src": name}, data=offer_sdp,
                                headers={"Content-Type": "application/sdp"}, timeout=to) as r:
            body = await r.text()
            if r.status >= 400 or "v=0" not in body:
                raise RuntimeError(f"go2rtc WebRTC: HTTP {r.status} {body[:120]}")
            return body
