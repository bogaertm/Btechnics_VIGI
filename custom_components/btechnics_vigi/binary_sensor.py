"""Verbinding per kanaal (camera online volgens de recorder)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coord = hass.data[DOMAIN][entry.entry_id]
    ents = [VigiOnline(coord, d["channel"]) for d in coord.data or []]
    if getattr(coord, "events_enabled", False):
        ents += [VigiMotion(coord, d["channel"], k) for d in coord.data or [] for k in ("beweging", "persoon", "voertuig")]
    async_add_entities(ents)


class VigiOnline(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, channel: int) -> None:
        super().__init__(coord)
        self.channel = channel
        self._attr_unique_id = f"{coord.entry.entry_id}_online_{channel}"
        self._attr_device_info = device_info(coord)

    @property
    def name(self) -> str:
        d = self.coordinator.channel(self.channel)
        return f"{d['name'] if d else f'Kanaal {self.channel}'} verbinding"

    @property
    def is_on(self) -> bool:
        d = self.coordinator.channel(self.channel)
        return bool(d and d["online"])


# ---- bewegingsmeldingen (events.py) ----
from homeassistant.helpers.dispatcher import async_dispatcher_connect  # noqa: E402
from homeassistant.core import callback  # noqa: E402
from homeassistant.helpers.event import async_call_later  # noqa: E402

from .events import HOLD_S, SIGNAL  # noqa: E402

_KIND = {"beweging": ("beweging", BinarySensorDeviceClass.MOTION, True),
         "persoon": ("persoon", BinarySensorDeviceClass.OCCUPANCY, True),
         "voertuig": ("voertuig", BinarySensorDeviceClass.MOTION, False)}


class VigiMotion(BinarySensorEntity):
    """Aan zolang er de laatste 30 s een melding van de recorder kwam (beweging, persoon of voertuig)."""
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coord, channel: int, kind: str) -> None:
        self.coord = coord
        self.channel = channel
        self.kind = kind
        label, dc, enabled = _KIND[kind]
        self._label = label
        self._attr_device_class = dc
        self._attr_entity_registry_enabled_default = enabled
        self._attr_unique_id = f"{coord.entry.entry_id}_{kind}_{channel}"
        self._attr_device_info = device_info(coord)
        self._off = None

    @property
    def name(self) -> str:
        d = self.coord.channel(self.channel)
        return f"{d['name'] if d else f'Kanaal {self.channel}'} {self._label}"

    @property
    def is_on(self) -> bool:
        import time  # noqa: PLC0415
        t = self.coord.events.last.get((self.channel, self.kind))
        return bool(t and time.time() - t < HOLD_S)

    @property
    def extra_state_attributes(self):
        from homeassistant.util import dt as dt_util  # noqa: PLC0415
        t = self.coord.events.last.get((self.channel, self.kind))
        return {"laatste_melding": dt_util.utc_from_timestamp(t).isoformat() if t else None}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL.format(self.coord.entry.entry_id), self._update))
        self.async_on_remove(self._cancel)

    @callback
    def _cancel(self) -> None:
        if self._off:
            self._off()
            self._off = None

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()
        self._cancel()
        if self.is_on:
            self._off = async_call_later(self.hass, HOLD_S + 0.5, self._later)

    @callback
    def _later(self, _now) -> None:
        self._off = None
        self.async_write_ha_state()
