"""Client tegen de nagebootste recorder."""
import aiohttp
import pytest

from custom_components.btechnics_vigi.api import (VigiAuthError, VigiClient, digest_header, merge_segments,
                                                   parse_challenge, utc_stamp)
from tests.fake_nvr import PASS, USER


def client(session, nvr, password=PASS):
    return VigiClient(session, "127.0.0.1", USER, password, port=nvr.port, scheme="http")


async def test_aanmelden_en_toestellen_gedecodeerd(nvr):
    async with aiohttp.ClientSession() as s:
        devs = await client(s, nvr).devices()
    assert devs[0] == {"channel": 1, "name": "Opslag", "alias": "IPC", "online": True, "ip": "192.168.6.81", "mac": "x"}
    assert devs[1]["name"] == "Cafe toog" and devs[1]["online"] is False


async def test_fout_wachtwoord(nvr):
    async with aiohttp.ClientSession() as s:
        with pytest.raises(VigiAuthError):
            await client(s, nvr, "fout").devices()


async def test_token_verlopen_wordt_vernieuwd(nvr):
    async with aiohttp.ClientSession() as s:
        c = client(s, nvr)
        await c.devices()
        nvr.expire_next = True
        assert len(await c.devices()) == 2


async def test_zoeken_pagineert_en_voegt_samen(nvr):
    async with aiohttp.ClientSession() as s:
        segs = await client(s, nvr).record_search(1, "20261030")
    assert [(x["start"], x["end"]) for x in segs] == [(1793350000, 1793354500), (1793360000, 1793360100)]
    assert segs[0]["parts"] == [[1793350000, 1793354500]]   # aansluitend = een deel
    assert nvr.calls.count("record/search/results") == 2


async def test_dagen_en_snapshot(nvr):
    async with aiohttp.ClientSession() as s:
        c = client(s, nvr)
        assert await c.record_days(1, "202610", "202610") == ["20261029", "20261030"]
        assert (await c.snapshot(1)).startswith(b"\xff\xd8")


def test_urls_met_speciale_tekens_in_wachtwoord():
    c = VigiClient(None, "192.168.6.80", "homeassistant", "a@b&c:d/e")
    assert c.live_url(3, 2) == "rtsp://homeassistant:a%40b%26c%3Ad%2Fe@192.168.6.80:554/live/3/2/avm"
    assert c.replay_url(1, 1793350000, 1793350060).endswith(
        "/replay/1/1/avm?starttime=20261030t084640z&endtime=20261030t084740z")


def test_utc_stamp_voorbeeld_uit_document():
    # document: 19701111t123059z = 1970-11-11 12:30:59 UTC
    assert utc_stamp(27174659) == "19701111t123059z"


def test_digest_zoals_document():
    ch = parse_challenge('Digest realm="TP-LINK NVR", nonce="abc", algorithm="SHA-256", url="/openapi/token"')
    assert ch["realm"] == "TP-LINK NVR" and ch["algorithm"] == "SHA-256"
    h = digest_header(ch, "admin", "pw", "GET", "/openapi/token")
    assert 'username="admin"' in h and "qop" not in h


def test_merge_gap():
    # tot 20 s ertussen = een stuk (HA verdraagt 30 s tijdsprong), meer = apart
    assert merge_segments([{"start": 0, "end": 10}, {"start": 30, "end": 40}]) == [
        {"start": 0, "end": 40, "parts": [[0, 10], [30, 40]]}]
    assert [(x["start"], x["end"]) for x in merge_segments([{"start": 0, "end": 10}, {"start": 31, "end": 40}])] == [
        (0, 10), (31, 40)]


async def test_zoekproces_wordt_hergebruikt(nvr):
    """Echte recorder: max 4 zoekprocessen (-71101). Zes zoekopdrachten mogen er maar een gebruiken."""
    async with aiohttp.ClientSession() as s:
        c = client(s, nvr)
        for day in ("20261025", "20261026", "20261027", "20261028", "20261029", "20261030"):
            await c.record_search(3, day)
    assert nvr.calls.count("record/search/free_process") == 1


async def test_zoeken_wacht_als_recorder_nog_zoekt(nvr, monkeypatch):
    import asyncio as aio
    real = aio.sleep
    monkeypatch.setattr("custom_components.btechnics_vigi.api.asyncio.sleep", lambda s: real(0))
    nvr.busy_next = 2
    async with aiohttp.ClientSession() as s:
        segs = await client(s, nvr).record_search(1, "20261030")
    assert len(segs) == 2


async def test_vol_en_ongeldig_proces(nvr, monkeypatch):
    import asyncio as aio
    real = aio.sleep
    monkeypatch.setattr("custom_components.btechnics_vigi.api.asyncio.sleep", lambda s: real(0))
    nvr.procs = [1, 2, 3, 4]   # alle 4 bezet door iets anders
    async with aiohttp.ClientSession() as s:
        c = client(s, nvr)
        with pytest.raises(Exception):
            await c.record_search(1, "20261030")
        nvr.procs = [9]         # er komt er een vrij; ons oude id bestaat niet meer
        c._proc = 7
        assert len(await c.record_search(1, "20261030")) == 2
