"""Opnames afspelen: replay-RTSP van de recorder omzetten naar HLS met de stream-integratie van HA.

De recorder speelt een opname af op echte snelheid (RTSP replay, VIGI OpenAPI v1.4 hoofdstuk 5.3).
Elke keer afspelen = een nieuwe HLS-stream; HA stopt ze zelf na 30 s zonder kijker (stream IdleTimer).
"""
from __future__ import annotations

import logging
import time

from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.components.stream import HLS_PROVIDER, create_stream
from homeassistant.core import HomeAssistant

from .api import VigiClient
from .const import MAX_REPLAY_S

_LOGGER = logging.getLogger(__name__)
MAX_ACTIVE = 3


class ReplayManager:
    def __init__(self, hass: HomeAssistant, client: VigiClient) -> None:
        self.hass = hass
        self.client = client
        self._active: list[tuple[float, object]] = []

    async def start(self, channel: int, start: int, end: int | None = None) -> str:
        end = end or start + 3600
        end = min(end, start + MAX_REPLAY_S)
        if end <= start:
            raise ValueError("einde moet na het begin liggen")
        url = self.client.replay_url(channel, start, end)
        # zonder audio (keuze Matthias 30/10/2026): G.711 van de recorder kan HLS toch niet dragen
        stream = create_stream(self.hass, url, {"rtsp_transport": "tcp"}, DynamicStreamSettings(),
                               stream_label=f"VIGI replay kanaal {channel}")
        stream.add_provider(HLS_PROVIDER)
        await stream.start()
        self._active.append((time.monotonic(), stream))
        await self._trim()
        return stream.endpoint_url(HLS_PROVIDER)

    async def _trim(self) -> None:
        while len(self._active) > MAX_ACTIVE:
            _, old = self._active.pop(0)
            try:
                await old.stop()
            except Exception:  # noqa: BLE001  opruimen mag nooit falen
                _LOGGER.debug("oude replay stoppen mislukt", exc_info=True)

    async def stop_all(self) -> None:
        while self._active:
            _, s = self._active.pop()
            try:
                await s.stop()
            except Exception:  # noqa: BLE001
                pass
