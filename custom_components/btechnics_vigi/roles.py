"""Rollen voor de Btechnics-koppelingen (zelfde bestand in Btechnics VTO en Btechnics VIGI).

Home Assistant kent zelf enkel beheerder en gewone gebruiker. Daarbovenop (keuze Matthias 30/10/2026):
- superadmin: beheerder in HA (Instellingen, HACS, alles);
- admin: gewone HA-gebruiker met alle beelden, opnames, foto's van de VTO, codebeheer en deur openen;
- medewerker: gewone HA-gebruiker met alle codes (ook de code zelf), codes aanmaken en stopzetten, deur openen
  en het logboek van de laatste 24 uur zonder foto's.
De rollen staan in .storage/btechnics_roles (gedeeld door beide koppelingen) en worden ingesteld in het dashboard
Gebruikers (kaart btechnics-vto-gebruikers, enkel superadmin) of in de instellingen van Btechnics VTO. Daar staat
ook per dashboard welke rollen het zien ("dashboards"). De koppelingen controleren de rol bij elke aanvraag.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

KEY = "btechnics_roles"
SUPER, ADMIN, MEDEWERKER, USER = "superadmin", "admin", "medewerker", "gebruiker"
LABEL = {SUPER: "Superadmin", ADMIN: "Admin", MEDEWERKER: "Medewerker", USER: "Gebruiker"}


async def async_load(hass: HomeAssistant) -> dict:
    """Rollen laden (één keer per HA-start, gedeeld)."""
    if KEY in hass.data:
        return hass.data[KEY]
    data = await Store(hass, 1, KEY).async_load() or {}
    roles = {ADMIN: list(data.get(ADMIN, [])), MEDEWERKER: list(data.get(MEDEWERKER, [])),
             "dashboards": dict(data.get("dashboards", {}))}
    hass.data[KEY] = roles
    return roles


async def async_save(hass: HomeAssistant, admins: list[str], medewerkers: list[str],
                     dashboards: dict | None = None) -> None:
    old = hass.data.get(KEY) or {}
    roles = {ADMIN: sorted(set(admins)), MEDEWERKER: sorted(set(medewerkers) - set(admins)),
             "dashboards": dict(old.get("dashboards", {}) if dashboards is None else dashboards)}
    await Store(hass, 1, KEY).async_save(roles)
    hass.data[KEY] = roles


async def async_set_role(hass: HomeAssistant, user_id: str, new_role: str) -> None:
    """Rol van één gebruiker in de opslag zetten (superadmin regelt HA zelf via de groep)."""
    cur = await async_load(hass)
    adm = [x for x in cur.get(ADMIN, []) if x != user_id]
    med = [x for x in cur.get(MEDEWERKER, []) if x != user_id]
    if new_role == ADMIN:
        adm.append(user_id)
    elif new_role == MEDEWERKER:
        med.append(user_id)
    await async_save(hass, adm, med)


def role(hass: HomeAssistant, user) -> str:
    if user is None:
        return USER
    if user.is_admin:
        return SUPER
    roles = hass.data.get(KEY) or {}
    if user.id in roles.get(ADMIN, []):
        return ADMIN
    if user.id in roles.get(MEDEWERKER, []):
        return MEDEWERKER
    return USER


def is_admin(hass: HomeAssistant, user) -> bool:
    """Superadmin of admin: alles zien (beelden, opnames, foto's, volledige historiek)."""
    return role(hass, user) in (SUPER, ADMIN)


def is_staff(hass: HomeAssistant, user) -> bool:
    """Superadmin, admin of medewerker: codes zien, aanmaken, stopzetten en deur openen."""
    return role(hass, user) in (SUPER, ADMIN, MEDEWERKER)
