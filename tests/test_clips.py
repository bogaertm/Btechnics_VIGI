"""Opnames binnenhalen (clips.py) met een nagebootste ffmpeg."""
import os
import sys
import time
from pathlib import Path

from custom_components.btechnics_vigi.api import VigiClient
from custom_components.btechnics_vigi.clips import ClipManager, split_pieces

FAKE = str(Path(__file__).parent / "fake_ffmpeg.py")


def test_split_pieces_volgt_deelstukken_en_venster():
    parts = [[100, 130], [140, 400]]
    p = split_pieces(parts, 110, 300, max_piece=120)
    assert p[0] == (110, 130)                       # begint op het gekozen tijdstip
    assert all(e - s <= 120 for s, e in p)
    assert p[-1][1] == 300 and sum(e - s for s, e in p) == 20 + 160   # geen gaten meegenomen


async def _wait(clip, sec=10):
    t = time.monotonic()
    while clip.state in ("fetching", "joining") and time.monotonic() - t < sec:
        import asyncio
        await asyncio.sleep(0.05)


async def test_clip_binnenhalen_en_tijdsindeling(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(ClipManager, "_binary", lambda self: FAKE)
    now = int(time.time())
    m = ClipManager(hass, VigiClient(None, "1.2.3.4", "u", "p@w"), tmp_path / "clips")
    clip = await m.start(3, "Cafe toog", now - 600, now - 400, [[now - 600, now - 560], [now - 550, now - 400]], "Matthias")
    await _wait(clip)
    st = clip.status()
    assert st["state"] == "ready", st
    assert st["codec"] == "hevc" and st["progress"] == 1
    assert [x["clock"] for x in st["map"]] == [s for s, _ in clip.pieces]
    assert abs(sum(x["len"] for x in st["map"]) - 190) < 1e-6
    assert clip.path.exists() and clip.size == 190
    assert not any(p.name.endswith(".tmp") for p in (tmp_path / "clips").iterdir())   # werkmap opgeruimd
    again = await m.start(3, "Cafe toog", now - 600, now - 400, [], "Matthias")
    assert again is clip                                                       # zelfde venster hergebruikt


async def test_clip_wacht_als_recorder_bezet(hass, tmp_path, monkeypatch):
    import asyncio
    real = asyncio.sleep
    monkeypatch.setattr("custom_components.btechnics_vigi.clips.asyncio.sleep", lambda s: real(0))
    monkeypatch.setattr(ClipManager, "_binary", lambda self: FAKE)
    busy = tmp_path / "busy"
    busy.write_text("1")
    monkeypatch.setenv("FAKE_BUSY_FILE", str(busy))
    now = int(time.time())
    m = ClipManager(hass, VigiClient(None, "1.2.3.4", "u", "p"), tmp_path / "clips")
    clip = await m.start(1, "Opslag", now - 100, now - 90, [], "x")
    await _wait(clip)
    assert clip.state == "ready"


async def test_clip_niet_in_de_toekomst_en_max_15_min(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(ClipManager, "_binary", lambda self: FAKE)
    now = int(time.time())
    m = ClipManager(hass, VigiClient(None, "1.2.3.4", "u", "p"), tmp_path / "clips")
    clip = await m.start(1, "Opslag", now - 3600, now + 3600, [], "x")
    assert clip.end - clip.start == 900
    await _wait(clip)
    import pytest
    with pytest.raises(ValueError):
        await m.start(1, "Opslag", now + 10, now + 60, [], "x")
