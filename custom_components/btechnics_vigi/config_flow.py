"""Instelscherm: IP, gebruiker en wachtwoord van de recorder; meteen getest via de OpenAPI.

Opnieuw aanmelden (wachtwoord gewijzigd) en opnieuw configureren (ander IP of gebruiker) zonder de koppeling te
verwijderen (audit 30/10/2026). Optie: camera-entiteiten (standaard uit, zie __init__.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VigiAuthError, VigiClient, VigiError
from .const import CONF_CAMERA_ENTITIES, DOMAIN


async def _test(hass, data: dict) -> tuple[dict[str, str], int]:
    client = VigiClient(async_get_clientsession(hass, verify_ssl=False),
                        data[CONF_HOST], data[CONF_USERNAME], data[CONF_PASSWORD])
    try:
        return {}, len(await client.devices())
    except VigiAuthError:
        return {"base": "invalid_auth"}, 0
    except VigiError:
        return {"base": "cannot_connect"}, 0


class VigiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return VigiOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, n = await _test(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"VIGI recorder {user_input[CONF_HOST]} ({n} camera's)",
                                               data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "192.168.6.80")): str,
            vol.Required(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, "homeassistant")): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors, _ = await _test(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reauth_confirm", errors=errors,
                                    data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
                                    description_placeholders={"user": entry.data[CONF_USERNAME]})

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            other = [e for e in self._async_current_entries()
                     if e.entry_id != entry.entry_id and e.unique_id == user_input[CONF_HOST]]
            errors, n = ({"base": "already_configured"}, 0) if other else await _test(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, unique_id=user_input[CONF_HOST], data=user_input,
                    title=f"VIGI recorder {user_input[CONF_HOST]} ({n} camera's)")
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
            vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class VigiOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_CAMERA_ENTITIES,
                         default=self.config_entry.options.get(CONF_CAMERA_ENTITIES, False)): bool,
        }))
