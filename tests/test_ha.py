"""Integratie in HA: instelscherm, entiteiten, WS-rechten."""
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.btechnics_vigi.api import VigiClient
from custom_components.btechnics_vigi.const import DOMAIN
from tests.fake_nvr import PASS, USER


@pytest.fixture
def via_fake(nvr, monkeypatch):
    monkeypatch.setattr(VigiClient, "base", property(lambda self: f"http://127.0.0.1:{nvr.port}"))
    return nvr


async def test_instelscherm(hass, via_fake):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r2 = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_HOST: "192.168.6.80", CONF_USERNAME: USER, CONF_PASSWORD: "fout"})
    assert r2["errors"] == {"base": "invalid_auth"}
    r3 = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_HOST: "192.168.6.80", CONF_USERNAME: USER, CONF_PASSWORD: PASS})
    assert r3["type"] is FlowResultType.CREATE_ENTRY and "2 camera" in r3["title"]


async def _setup(hass):
    await __import__("homeassistant.setup", fromlist=["x"]).async_setup_component(hass, "stream", {})
    e = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.6.80", CONF_USERNAME: USER, CONF_PASSWORD: PASS})
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def test_entiteiten_zonder_camera_entiteiten(hass, via_fake):
    """Standaard geen camera-entiteiten (audit: camera.record ook voor gewone gebruikers)."""
    await _setup(hass)
    assert hass.states.async_all("camera") == []
    off = [s for s in hass.states.async_all("binary_sensor") if s.state == "off" and "verbinding" in s.entity_id]
    assert len(off) == 1 and "toog" in off[0].entity_id


async def test_camera_entiteiten_als_optie(hass, via_fake):
    e = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.6.80", CONF_USERNAME: USER, CONF_PASSWORD: PASS},
                        options={"camera_entities": True})
    await __import__("homeassistant.setup", fromlist=["x"]).async_setup_component(hass, "stream", {})
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    cams = hass.states.async_all("camera")
    assert len(cams) == 2
    assert [c for c in cams if "toog" in c.entity_id][0].state == "unavailable"


async def test_wachtwoord_gewijzigd_vraagt_opnieuw_aanmelden(hass, via_fake, monkeypatch):
    e = await _setup(hass)
    monkeypatch.setattr("tests.fake_nvr.PASS", "nieuw")   # recorder aanvaardt het oude wachtwoord niet meer
    import tests.fake_nvr as f
    via_fake.tokens.clear()
    coord = hass.data[DOMAIN][e.entry_id]
    coord.client._access = None
    coord.client._refresh = None
    await coord.async_refresh()
    await hass.async_block_till_done()
    flows = [x for x in hass.config_entries.flow.async_progress() if x["context"]["source"] == "reauth"]
    assert flows, "geen reauth gestart"
    r = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {CONF_PASSWORD: "nieuw"})
    assert r["reason"] == "reauth_successful"
    assert e.data[CONF_PASSWORD] == "nieuw"


async def test_ws_rechten(hass, via_fake, hass_ws_client, hass_admin_user):
    await _setup(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/record/days", "channel": 1})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["days"] == ["20261029", "20261030"]
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/record/search", "channel": 1, "day": "20261030"})
    r = await ws.receive_json()
    assert len(r["result"]["segments"]) == 2
    await ws.send_json({"id": 3, "type": f"{DOMAIN}/probe", "path": "/openapi/systemctl"})
    r = await ws.receive_json()
    assert not r["success"]
    hass_admin_user.groups = []  # gebruiker zonder beheerrechten
    await ws.send_json({"id": 4, "type": f"{DOMAIN}/record/days", "channel": 1})
    r = await ws.receive_json()
    assert not r["success"] and r["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 5, "type": f"{DOMAIN}/channels"})
    r = await ws.receive_json()
    assert r["success"] and len(r["result"]["channels"]) == 2 and r["result"]["admin"] is False
    assert "ip" not in r["result"]["channels"][0] and r["result"]["host"] is None
    n = 10
    for cmd in ({"type": f"{DOMAIN}/record/search", "channel": 1, "day": "20261030"},
                {"type": f"{DOMAIN}/replay", "channel": 1, "start": 1793350000},
                {"type": f"{DOMAIN}/replay/stop"},
                {"type": f"{DOMAIN}/live", "channel": 1},
                {"type": f"{DOMAIN}/webrtc", "channel": 1, "offer": "v=0"},
                {"type": f"{DOMAIN}/ice"},
                {"type": f"{DOMAIN}/clip/start", "channel": 1, "start": 1793350000, "end": 1793350060},
                {"type": f"{DOMAIN}/clip/status", "clip_id": "x"},
                {"type": f"{DOMAIN}/clip/cancel"},
                {"type": f"{DOMAIN}/probe", "path": "/openapi/module_list"},
                {"type": f"{DOMAIN}/replay_probe", "channel": 1, "start": 1, "end": 2}):
        n += 1
        await ws.send_json({"id": n, **cmd})
        r = await ws.receive_json()
        assert not r["success"] and r["error"]["code"] == "unauthorized", cmd


async def test_optie_aan_en_uit_zonder_vastlopen(hass, via_fake):
    """Review 30/10/2026: unload rekende met de nieuwe opties en liep vast (FAILED_UNLOAD)."""
    from homeassistant.config_entries import ConfigEntryState
    await __import__("homeassistant.setup", fromlist=["x"]).async_setup_component(hass, "camera", {})
    e = await _setup(hass)
    hass.config_entries.async_update_entry(e, options={"camera_entities": True})
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED and len(hass.states.async_all("camera")) == 2
    hass.config_entries.async_update_entry(e, options={"camera_entities": False})
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED


async def test_meldingen_van_de_recorder(hass, via_fake, hass_client_no_auth, hass_ws_client):
    """Event server wordt ingesteld (andere server blijft), push van de recorder zet de sensor aan en komt op de tijdlijn."""
    import time
    from unittest.mock import patch
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    await __import__("homeassistant.setup", fromlist=["x"]).async_setup_component(hass, "http", {})
    with patch("homeassistant.components.network.async_get_source_ip", return_value="192.168.6.138"):
        e = await _setup(hass)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
    coord = hass.data[DOMAIN][e.entry_id]
    ours = [s for s in via_fake.servers if s["id"] != 1]
    assert len(via_fake.servers) == 2 and ours[0]["ip_or_domain"] == "192.168.6.138", via_fake.servers
    assert "actief" in coord.events.status
    got = []
    hass.bus.async_listen("btechnics_vigi_event", lambda ev: got.append(ev.data))
    client = await hass_client_no_auth()
    body = (b'----VIGI-OPENAPI-MSG-PUSH\r\nContent-Disposition: form-data;name="event"\r\n'
            b'Content-Type: application/json; charset=UTF-8\r\n\r\n'
            b'{"device_id":"x","device_name":"NVR","messages":[{"type":1,"sub_type":[2,101],'
            b'"localtime":"2026-10-30 22:31:05","channel":"1"}]}\r\n----VIGI-OPENAPI-MSG-PUSH--\r\n')
    url = "/api/btechnics_vigi/event/" + coord.events.token
    hdr = {"Content-Type": "multipart/form-data;boundary=--VIGI-OPENAPI-MSG-PUSH"}
    r = await client.post(url, data=body, headers=hdr)
    assert r.status == 403                                   # testclient is niet het IP van de recorder
    r = await client.post("/api/btechnics_vigi/event/fout", data=body, headers=hdr)
    assert r.status == 404
    with patch.object(type(coord.events), "allowed", lambda self, remote: True):
        r = await client.post(url, data=body, headers=hdr)
    assert r.status == 200
    await hass.async_block_till_done()
    assert got and got[0]["kind"] == "persoon" and got[0]["channel_name"] == "Opslag"
    assert hass.states.get("binary_sensor.vigi_recorder_opslag_persoon").state == "on"
    assert hass.states.get("binary_sensor.vigi_recorder_opslag_beweging").state == "on"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/events", "channel": 1, "day": "20261030"})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["events"][0][1] == "persoon"
