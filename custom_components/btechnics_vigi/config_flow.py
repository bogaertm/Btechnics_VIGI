"""Instelscherm: IP, gebruiker en wachtwoord van de recorder; meteen getest via de OpenAPI."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VigiAuthError, VigiClient, VigiError
from .const import DOMAIN

SCHEMA = vol.Schema({
    vol.Required(CONF_HOST, default="192.168.6.80"): str,
    vol.Required(CONF_USERNAME, default="homeassistant"): str,
    vol.Required(CONF_PASSWORD): str,
})


class VigiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = VigiClient(async_get_clientsession(self.hass, verify_ssl=False),
                                user_input[CONF_HOST], user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                devices = await client.devices()
            except VigiAuthError:
                errors["base"] = "invalid_auth"
            except VigiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"VIGI recorder {user_input[CONF_HOST]} ({len(devices)} camera's)", data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
