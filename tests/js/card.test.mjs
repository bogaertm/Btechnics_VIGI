// Tijdrekening van de kaart (dayStart, uren rond zomer/wintertijd, klok <-> bestand). Uitvoeren: node tests/js/card.test.mjs
import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const src = fs.readFileSync(new URL("../../custom_components/btechnics_vigi/frontend/btechnics-vigi-card.js", import.meta.url), "utf8");
class HTMLElement { addEventListener() {} }
const registry = new Map();
const ctx = { HTMLElement, window: {}, document: { querySelectorAll: () => [] }, customElements: { get: (n) => registry.get(n), define: (n, c) => registry.set(n, c) },
  setTimeout: () => 0, setInterval: () => 0, clearInterval() {}, Intl, Date, Math, console, IntersectionObserver: class {}, URL };
ctx.window = ctx; ctx.window.addEventListener = () => {};
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__t = { dayStart, addDays, localToEpoch, BtechnicsVigiCard };", ctx);
const { dayStart, addDays, BtechnicsVigiCard } = ctx.__t;
let ok = 0;
const tz = "Europe/Brussels";
// gewone dag: 30/10/2026 00:00 CET = 29/10 23:00 UTC
assert.equal(dayStart("20261030", tz), Date.UTC(2026, 9, 29, 23) / 1000); ok++;
// wintertijd 25/10/2026: dag van 25 uur
assert.equal(dayStart("20261026", tz) - dayStart("20261025", tz), 25 * 3600); ok++;
// zomertijd 29/03/2026: dag van 23 uur
assert.equal(dayStart("20260330", tz) - dayStart("20260329", tz), 23 * 3600); ok++;
assert.equal(addDays("20261231", 1), "20270101"); ok++;
// uren van de kaart op 25/10: 25 uren, uur 3 (index 3) begint om 02u00 CET
const c = new BtechnicsVigiCard();
c._hass = { config: { time_zone: tz } };
c._sel = { day: "20261025", hour: null };
assert.equal(c._hourOffsets().length, 25); ok++;
c._sel.day = "20260329";
assert.equal(c._hourOffsets().length, 23); ok++;
// klok <-> plaats in het bestand met een gat tussen twee delen
c._cur = { start: 1000, clip: { map: [{ clock: 1000, len: 20 }, { clock: 1050, len: 30 }] } };
assert.equal(c._clockToMedia(1010), 10); ok++;
assert.equal(c._clockToMedia(1030), 20); ok++;   // in het gat: begin van het volgende deel
assert.equal(c._clockToMedia(1060), 30); ok++;
assert.equal(c._mediaToClock(25), 1055); ok++;
assert.equal(c._mediaToClock(60), 1080); ok++;
console.log(`card: ${ok} ok`);
