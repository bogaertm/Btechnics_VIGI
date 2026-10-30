import pytest
from aiohttp.test_utils import TestServer

from tests.fake_nvr import FakeNVR

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def nvr(socket_enabled):
    f = FakeNVR()
    srv = TestServer(f.app())
    await srv.start_server()
    f.port = srv.port
    yield f
    await srv.close()
