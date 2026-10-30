"""Rollen voor de Btechnics-koppelingen (zelfde bestand in Btechnics VTO en Btechnics VIGI), enkel lezen.

Home Assistant kent zelf enkel beheerder en gewone gebruiker, zonder rechten per rol
(github.com/orgs/home-assistant/discussions/1171). Daarom (keuze Matthias 30/10/2026):
- superadmin: beheerder in HA (Instellingen, HACS, alles);
- admin: gewone HA-gebruiker met alle beelden, opnames, foto's van de VTO, codebeheer en deur openen;
- medewerker: gewone HA-gebruiker met alle codes (ook de code zelf), codes aanmaken en stopzetten, deur openen
  en het logboek van de laatste 24 uur zonder foto's.

De rollen worden beheerd in de Work-app en bewaard door Btechnics IOT (domein btechnics_branding, vanaf v1.49.0)
in .storage/btechnics_roles. Btechnics IOT laadt ze in hass.data["btechnics_roles"] (altijd hetzelfde object) en
vuurt btechnics_roles_changed bij elke wijziging. Deze koppeling leest enkel en controleert zelf de rechten bij
elke aanvraag. Ontbreekt de sleutel (oudere Btechnics IOT), dan is iedereen behalve beheerders "gebruiker".
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

KEY = "btechnics_roles"
SUPER, ADMIN, MEDEWERKER, USER = "superadmin", "admin", "medewerker", "gebruiker"
LABEL = {SUPER: "Superadmin", ADMIN: "Admin", MEDEWERKER: "Medewerker", USER: "Gebruiker"}


def role(hass: HomeAssistant, user) -> str:
    if user is None:
        return USER
    if user.is_admin:
        return SUPER
    roles = hass.data.get(KEY) or {}
    if user.id in (roles.get(ADMIN) or []):
        return ADMIN
    if user.id in (roles.get(MEDEWERKER) or []):
        return MEDEWERKER
    return USER


def is_admin(hass: HomeAssistant, user) -> bool:
    """Superadmin of admin: alles zien (beelden, opnames, foto's, volledige historiek)."""
    return role(hass, user) in (SUPER, ADMIN)


def is_staff(hass: HomeAssistant, user) -> bool:
    """Superadmin, admin of medewerker: codes zien, aanmaken, stopzetten en deur openen."""
    return role(hass, user) in (SUPER, ADMIN, MEDEWERKER)
