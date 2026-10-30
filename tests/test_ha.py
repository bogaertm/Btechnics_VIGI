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


async def test_entiteiten(hass, via_fake):
    await _setup(hass)
    cams = sorted(s.entity_id for s in hass.states.async_all("camera"))
    assert len(cams) == 2
    off = [s for s in hass.states.async_all("binary_sensor") if s.state == "off"]
    assert len(off) == 1 and "toog" in off[0].entity_id
    cafe = hass.states.get([c for c in cams if "toog" in c][0])
    assert cafe.state == "unavailable"


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
