/* Btechnics VIGI: dashboardkaart met live raster en opnames (tijdlijn + speler).
 * Opnames enkel voor beheerders (backend weigert ook: btechnics_vigi/record/* en replay zijn admin-only).
 * Speler: ha-hls-player van Home Assistant (haalt verbinding uit de context van de pagina, dus enkel binnen een dashboard).
 */
const VIGI_VERSION = "0.2.5";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");

function tzParts(ms, tz) {
  const f = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const o = {};
  for (const p of f.formatToParts(new Date(ms))) o[p.type] = p.value;
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour, mi: +o.minute, s: +o.second };
}
// begin van een lokale dag (YYYYMMDD) als epoch-seconden in tijdzone tz, ook rond zomer/wintertijd
function dayStart(day, tz) {
  const y = +day.slice(0, 4), m = +day.slice(4, 6), d = +day.slice(6, 8);
  let t = Date.UTC(y, m - 1, d, 0, 0, 0);
  for (let i = 0; i < 3; i++) {
    const p = tzParts(t, tz);
    const asUtc = Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s);
    t -= asUtc - Date.UTC(y, m - 1, d, 0, 0, 0);
  }
  return Math.round(t / 1000);
}
function nextDay(day) {
  const t = new Date(Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8) + 1));
  return `${t.getUTCFullYear()}${pad(t.getUTCMonth() + 1)}${pad(t.getUTCDate())}`;
}
function todayStr(tz) { const p = tzParts(Date.now(), tz); return `${p.y}${pad(p.mo)}${pad(p.d)}`; }
function hms(sec, tz) { const p = tzParts(sec * 1000, tz); return `${pad(p.h)}u${pad(p.mi)}m${pad(p.s)}`; }
function dayLabel(day) {
  const t = new Date(Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8), 12));
  return t.toLocaleDateString("nl-BE", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
}
function dur(s) { s = Math.round(s); return s >= 3600 ? `${Math.floor(s / 3600)}u${pad(Math.floor((s % 3600) / 60))}` : s >= 60 ? `${Math.floor(s / 60)} min` : `${s} s`; }

class BtechnicsVigiCard extends HTMLElement {
  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }
  static getStubConfig() { return {}; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    else this._updateLive();
  }

  connectedCallback() { if (this._hass && !this._timer) this._startTimer(); }
  disconnectedCallback() { clearInterval(this._timer); this._timer = null; this._stopPlayer(); }

  get _tz() { return (this._hass && this._hass.config && this._hass.config.time_zone) || Intl.DateTimeFormat().resolvedOptions().timeZone; }
  get _admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

  async _init() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._tab = "live";
    this._sel = { channel: null, day: todayStr(this._tz) };
    this._render();
    try {
      const r = await this._hass.callWS({ type: "btechnics_vigi/channels" });
      this._channels = r.channels || [];
    } catch (e) {
      this._err = "Recorder niet bereikbaar: " + (e.message || e.code || e);
      this._channels = [];
    }
    if (this._channels.length) this._sel.channel = this._channels[0].channel;
    this._render();
    this._startTimer();
  }

  _startTimer() {
    clearInterval(this._timer);
    this._timer = setInterval(() => this._refreshSnaps(), 3000);
  }

  _cams() {
    const st = this._hass.states;
    return Object.keys(st).filter((e) => e.startsWith("camera.") && st[e].attributes && st[e].attributes.kanaal != null)
      .map((e) => ({ entity: e, state: st[e], ch: st[e].attributes.kanaal }))
      .sort((a, b) => a.ch - b.ch);
  }

  _render() {
    const r = this.shadowRoot;
    if (!r) return;
    const tabs = [["live", "Live"]].concat(this._admin ? [["rec", "Opnames"]] : []);
    r.innerHTML = `<style>${CSS}</style><ha-card>
      <div class="top"><div class="tabs">${tabs.map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
      <span class="ver">Btechnics VIGI ${VIGI_VERSION}</span></div>
      ${this._err ? `<div class="err">${esc(this._err)}</div>` : ""}
      <div id="body"></div></ha-card>`;
    r.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { this._tab = b.dataset.tab; this._stopPlayer(); this._render(); }));
    if (this._tab === "rec" && this._admin) this._renderRec(); else this._renderLive();
  }

  _camName(entity) {
    const s = this._hass.states[entity];
    const ch = s && s.attributes.kanaal;
    const c = (this._channels || []).find((x) => x.channel === ch);
    if (c && c.name) return c.name;
    return ((s && s.attributes.friendly_name) || entity).replace(/^VIGI recorder\s+/i, "");
  }

  /* ---------------- Live ---------------- */
  _renderLive() {
    const body = this.shadowRoot.getElementById("body");
    const cams = this._cams();
    if (!cams.length) { body.innerHTML = `<div class="empty">Geen camera's gevonden.</div>`; return; }
    body.innerHTML = `<div class="grid">${cams.map((c) => `
      <div class="tile" data-e="${esc(c.entity)}">
        <div class="img"><img alt="" data-e="${esc(c.entity)}"><span class="off">Niet bereikbaar</span></div>
        <div class="cap"><span class="dot"></span><b>${esc(this._camName(c.entity))}</b><span class="ch">kanaal ${esc(c.ch)}</span></div>
      </div>`).join("")}</div>
      <div class="hint">Beelden vernieuwen om de 3 seconden. Klik op een camera voor live beeld.</div>
      <div class="modal" id="modal" hidden><div class="mbox"><div class="mhead"><b id="mtitle"></b><button id="mclose" title="Sluiten">✕</button></div><div id="mlive"></div></div></div>`;
    body.querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => this._openLive(t.dataset.e)));
    body.querySelector("#mclose").addEventListener("click", () => this._closeLive());
    body.querySelector("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") this._closeLive(); });
    this._updateLive();
    this._refreshSnaps();
  }

  _updateLive() {
    if (this._tab !== "live" || !this.shadowRoot) return;
    for (const t of this.shadowRoot.querySelectorAll(".tile")) {
      const s = this._hass.states[t.dataset.e];
      t.classList.toggle("unavail", !s || s.state === "unavailable");
    }
    const m = this.shadowRoot.querySelector("ha-camera-stream");
    if (m && this._hass.states[this._liveEntity]) m.stateObj = this._hass.states[this._liveEntity];
  }

  _refreshSnaps() {
    if (this._tab !== "live" || !this.shadowRoot || document.hidden) return;
    if (this._liveEntity) return; // tijdens live beeld geen extra belasting
    for (const img of this.shadowRoot.querySelectorAll(".tile img")) {
      const s = this._hass.states[img.dataset.e];
      if (!s || s.state === "unavailable" || !s.attributes.entity_picture) continue;
      if (img._busy) continue;
      img._busy = true;
      const nu = new Image();
      nu.onload = () => { img.src = nu.src; img._busy = false; };
      nu.onerror = () => { img._busy = false; };
      nu.src = s.attributes.entity_picture + "&t=" + Date.now();
    }
  }

  _openLive(entity) {
    this._liveEntity = entity;
    const m = this.shadowRoot.getElementById("modal");
    this.shadowRoot.getElementById("mtitle").textContent = this._camName(entity) + " live";
    const host = this.shadowRoot.getElementById("mlive");
    host.innerHTML = "";
    const cs = document.createElement("ha-camera-stream");
    cs.hass = this._hass;
    cs.stateObj = this._hass.states[entity];
    cs.controls = true;
    cs.muted = true;
    cs.allowExoPlayer = true;
    host.appendChild(cs);
    m.hidden = false;
  }

  _closeLive() {
    this._liveEntity = null;
    const m = this.shadowRoot.getElementById("modal");
    if (m) { m.hidden = true; this.shadowRoot.getElementById("mlive").innerHTML = ""; }
  }

  /* ---------------- Opnames ---------------- */
  _renderRec() {
    const body = this.shadowRoot.getElementById("body");
    const chs = this._channels || [];
    body.innerHTML = `
      <div class="bar">
        <div class="chips">${chs.map((c) => `<button class="chip ${c.channel === this._sel.channel ? "on" : ""}" data-ch="${c.channel}">${esc(c.name)}</button>`).join("")}</div>
        <div class="days"><button class="nav" id="prev" title="Vorige dag">‹</button>
          <input type="date" id="date"><button class="nav" id="next" title="Volgende dag">›</button>
          <button class="nav" id="today">Vandaag</button></div>
      </div>
      <div id="avail" class="sub"></div>
      <div class="tl" id="tl"><div class="tlbar" id="tlbar"></div><div class="hours">${[0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${h}u</span>`).join("")}</div></div>
      <div class="sub" id="tlinfo">Klik op de tijdlijn of op een stuk om af te spelen.</div>
      <div class="playwrap" id="playwrap" hidden>
        <div class="phead"><b id="ptitle"></b><span class="sub" id="pnote"></span></div>
        <div id="player"></div>
        <div class="pbtn"><button id="back">−1 min</button><button id="fwd">+1 min</button><button id="nextseg">Volgend stuk ›</button><button id="pstop">Stop</button></div>
      </div>
      <details class="segs" id="segsd"><summary>Lijst van de stukken</summary><div id="segs"></div></details>`;
    body.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => { this._sel.channel = +b.dataset.ch; this._stopPlayer(); this._renderRec(); }));
    const di = body.querySelector("#date");
    const d = this._sel.day;
    di.value = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    di.max = (() => { const t = todayStr(this._tz); return `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}`; })();
    di.addEventListener("change", () => { if (di.value) { this._sel.day = di.value.replace(/-/g, ""); this._stopPlayer(); this._renderRec(); } });
    body.querySelector("#prev").addEventListener("click", () => this._shiftDay(-1));
    body.querySelector("#next").addEventListener("click", () => this._shiftDay(1));
    body.querySelector("#today").addEventListener("click", () => { this._sel.day = todayStr(this._tz); this._stopPlayer(); this._renderRec(); });
    body.querySelector("#tlbar").addEventListener("click", (ev) => this._clickTimeline(ev));
    body.querySelector("#back").addEventListener("click", () => this._play(this._cur - 60));
    body.querySelector("#fwd").addEventListener("click", () => this._play(this._cur + 60));
    body.querySelector("#nextseg").addEventListener("click", () => {
      const n = (this._segs || []).find((s) => s.start > this._cur + 5);
      if (n) this._play(n.start);
    });
    body.querySelector("#pstop").addEventListener("click", () => this._stopPlayer(true));
    this._loadDays();
    this._loadSegs();
  }

  _shiftDay(n) {
    let d = this._sel.day;
    if (n > 0) d = nextDay(d);
    else { const t = new Date(Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8) - 1)); d = `${t.getUTCFullYear()}${pad(t.getUTCMonth() + 1)}${pad(t.getUTCDate())}`; }
    if (d > todayStr(this._tz)) return;
    this._sel.day = d;
    this._stopPlayer();
    this._renderRec();
  }

  async _loadDays() {
    const el = this.shadowRoot.getElementById("avail");
    const key = this._sel.channel;
    if (this._daysFor !== key) {
      this._days = null;
      try { this._days = (await this._hass.callWS({ type: "btechnics_vigi/record/days", channel: key })).days; this._daysFor = key; }
      catch (e) { el.textContent = "Dagen met opnames ophalen mislukt: " + (e.message || e.code); return; }
    }
    if (!el || !this._days) return;
    const has = this._days.includes(this._sel.day);
    el.innerHTML = this._days.length
      ? `Opnames van ${esc(dayLabel(this._days[0]))} tot ${esc(dayLabel(this._days[this._days.length - 1]))}${has ? "" : ` · <b>geen opnames op ${esc(dayLabel(this._sel.day))}</b>`}`
      : "Geen opnames gevonden voor deze camera (laatste 3 maanden).";
  }

  async _loadSegs() {
    const ch = this._sel.channel, day = this._sel.day;
    const info = this.shadowRoot.getElementById("tlinfo");
    this._segs = [];
    this._drawTimeline();
    if (ch == null) return;
    info.textContent = "Opnames zoeken…";
    try {
      const r = await this._hass.callWS({ type: "btechnics_vigi/record/search", channel: ch, day });
      if (ch !== this._sel.channel || day !== this._sel.day) return;
      this._segs = r.segments || [];
    } catch (e) {
      info.textContent = "Zoeken mislukt: " + (e.message || e.code);
      return;
    }
    const tot = this._segs.reduce((a, s) => a + (s.end - s.start), 0);
    info.textContent = this._segs.length
      ? `${this._segs.length} stukken, samen ${dur(tot)} opgenomen. Klik op de tijdlijn of op een stuk om af te spelen.`
      : "Geen opnames op deze dag.";
    this._drawTimeline();
    const list = this.shadowRoot.getElementById("segs");
    list.innerHTML = this._segs.map((s, i) => `<button class="seg" data-i="${i}">${hms(s.start, this._tz)} tot ${hms(s.end, this._tz)} <span class="sub">(${dur(s.end - s.start)})</span></button>`).join("") || `<div class="sub">Geen.</div>`;
    list.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => this._play(this._segs[+b.dataset.i].start)));
  }

  _drawTimeline() {
    const bar = this.shadowRoot.getElementById("tlbar");
    if (!bar) return;
    const d0 = dayStart(this._sel.day, this._tz), d1 = dayStart(nextDay(this._sel.day), this._tz), L = d1 - d0;
    const now = Math.floor(Date.now() / 1000);
    bar.innerHTML = (this._segs || []).map((s) => {
      const a = Math.max(0, (s.start - d0) / L) * 100, w = Math.max(0.15, ((Math.min(s.end, d1) - Math.max(s.start, d0)) / L) * 100);
      return `<i style="left:${a}%;width:${w}%"></i>`;
    }).join("") + (now > d0 && now < d1 ? `<u style="left:${((now - d0) / L) * 100}%" title="nu"></u>` : "")
      + (this._cur && this._cur >= d0 && this._cur < d1 ? `<em style="left:${((this._cur - d0) / L) * 100}%"></em>` : "");
  }

  _clickTimeline(ev) {
    const bar = ev.currentTarget, rect = bar.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
    const d0 = dayStart(this._sel.day, this._tz), d1 = dayStart(nextDay(this._sel.day), this._tz);
    let t = Math.floor(d0 + f * (d1 - d0));
    const segs = this._segs || [];
    const inSeg = segs.find((s) => t >= s.start && t < s.end);
    if (!inSeg) {
      const n = segs.find((s) => s.start > t);
      if (!n) { this.shadowRoot.getElementById("tlinfo").textContent = "Na dit tijdstip is er niets opgenomen op deze dag."; return; }
      t = n.start;
    }
    this._play(t);
  }

  async _play(t) {
    t = Math.floor(t);
    const segs = this._segs || [];
    const seg = segs.find((s) => t >= s.start && t < s.end) || segs.find((s) => s.start > t);
    if (!seg) return;
    if (t < seg.start) t = seg.start;
    this._cur = t;
    this._drawTimeline();
    const wrap = this.shadowRoot.getElementById("playwrap");
    wrap.hidden = false;
    const ch = (this._channels || []).find((c) => c.channel === this._sel.channel);
    this.shadowRoot.getElementById("ptitle").textContent = `${ch ? ch.name : "Kanaal " + this._sel.channel} · ${dayLabel(this._sel.day)} vanaf ${hms(t, this._tz)}`;
    const note = this.shadowRoot.getElementById("pnote");
    note.textContent = "Opname starten…";
    const host = this.shadowRoot.getElementById("player");
    host.innerHTML = "";
    const end = Math.min(t + 3600, dayStart(nextDay(this._sel.day), this._tz));
    const req = (this._req = (this._req || 0) + 1);
    let url;
    try {
      url = (await this._hass.callWS({ type: "btechnics_vigi/replay", channel: this._sel.channel, start: t, end })).url;
    } catch (e) {
      note.textContent = "Afspelen mislukt: " + (e.message || e.code);
      return;
    }
    if (req !== this._req) return;
    const p = document.createElement("ha-hls-player");
    p.hass = this._hass;
    // ha-hls-player bouwt adressen met new URL(..., this.url): een relatief adres geeft "Invalid base URL" (vastgesteld 30/10/2026)
    p.url = this._hass.hassUrl ? this._hass.hassUrl(url) : new URL(url, location.origin).href;
    p.controls = true;
    p.muted = true;
    p.autoPlay = true;
    p.playsInline = true;
    p.allowExoPlayer = true;
    host.appendChild(p);
    note.textContent = "Speelt af op normale snelheid, zonder geluid. Even wachten tot het beeld verschijnt.";
    this.shadowRoot.getElementById("playwrap").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  _stopPlayer(hide) {
    this._req = (this._req || 0) + 1;
    const host = this.shadowRoot && this.shadowRoot.getElementById("player");
    if (host) host.innerHTML = "";
    if (hide) {
      this._cur = null;
      const w = this.shadowRoot.getElementById("playwrap");
      if (w) w.hidden = true;
      this._drawTimeline();
    }
  }
}

const CSS = `
:host { display:block; }
ha-card { padding: 12px 16px 16px; }
.top { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
.tabs { display:flex; gap:6px; }
.tab { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color); padding:8px 16px; border-radius:18px; font:inherit; cursor:pointer; }
.tab.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
.ver, .sub, .hint { color:var(--secondary-text-color); font-size:0.85rem; }
.err { color:var(--error-color); margin:8px 0; }
.empty { padding:24px; color:var(--secondary-text-color); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:12px; }
.tile { border:1px solid var(--divider-color); border-radius:10px; overflow:hidden; cursor:pointer; background:var(--secondary-background-color); }
.tile .img { position:relative; aspect-ratio:16/9; background:#111; }
.tile img { width:100%; height:100%; object-fit:cover; display:block; }
.tile .off { display:none; position:absolute; inset:0; align-items:center; justify-content:center; color:#ddd; }
.tile.unavail .off { display:flex; } .tile.unavail img { opacity:.25; }
.cap { display:flex; align-items:center; gap:8px; padding:8px 10px; }
.cap .ch { margin-left:auto; color:var(--secondary-text-color); font-size:.8rem; }
.dot { width:8px; height:8px; border-radius:50%; background:var(--success-color, #2e7d32); flex:none; }
.tile.unavail .dot { background:var(--error-color, #c62828); }
.hint { margin-top:10px; }
.modal { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:10; display:flex; align-items:center; justify-content:center; padding:16px; }
.modal[hidden] { display:none; }
.mbox { background:var(--card-background-color); border-radius:12px; width:min(1100px, 100%); max-height:100%; overflow:auto; }
.mhead { display:flex; justify-content:space-between; align-items:center; padding:10px 14px; }
.mhead button { background:none; border:none; font-size:20px; cursor:pointer; color:var(--primary-text-color); width:40px; height:40px; }
.bar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; margin-bottom:8px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color); padding:6px 12px; border-radius:16px; font:inherit; cursor:pointer; min-height:36px; }
.chip.on { border-color:var(--primary-color); color:var(--primary-color); font-weight:600; }
.days { display:flex; gap:6px; align-items:center; }
.days input { font:inherit; padding:6px 8px; border:1px solid var(--divider-color); border-radius:8px; background:var(--card-background-color); color:var(--primary-text-color); min-height:36px; box-sizing:border-box; }
.nav { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color); border-radius:8px; min-width:36px; min-height:36px; font:inherit; cursor:pointer; padding:0 10px; }
.tl { margin:14px 0 4px; }
.tlbar { position:relative; height:34px; background:var(--secondary-background-color); border-radius:6px; cursor:crosshair; overflow:hidden; border:1px solid var(--divider-color); }
.tlbar i { position:absolute; top:0; bottom:0; background:var(--primary-color); opacity:.75; }
.tlbar u { position:absolute; top:0; bottom:0; width:2px; background:var(--error-color, #c62828); text-decoration:none; }
.tlbar em { position:absolute; top:-2px; bottom:-2px; width:3px; background:#ED6928; }
.hours { position:relative; height:18px; font-size:.75rem; color:var(--secondary-text-color); }
.hours span { position:absolute; transform:translateX(-50%); top:2px; }
.hours span:first-child { transform:none; } .hours span:last-child { transform:translateX(-100%); }
.playwrap { margin-top:12px; border:1px solid var(--divider-color); border-radius:10px; padding:10px; }
.playwrap[hidden] { display:none; }
.phead { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; margin-bottom:8px; }
#player { background:#000; border-radius:6px; overflow:hidden; min-height:120px; }
.pbtn { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
.pbtn button, .seg { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color); border-radius:8px; min-height:36px; padding:0 12px; font:inherit; cursor:pointer; }
.segs { margin-top:12px; }
.segs summary { cursor:pointer; color:var(--primary-color); padding:6px 0; }
#segs { display:flex; flex-wrap:wrap; gap:6px; max-height:260px; overflow:auto; padding-top:6px; }
@media (max-width: 600px) {
  ha-card { padding:10px; }
  .grid { grid-template-columns:1fr; }
  .bar { flex-direction:column; align-items:stretch; }
  .chips { flex-wrap:nowrap; overflow-x:auto; }
  .chip { white-space:nowrap; }
  .days input { flex:1; }
}
`;

// Registreren. Bij het laden van de pagina bleef de kaart soms onbekend terwijl het script wel liep
// ("Configuratiefout", vastgesteld 30/10/2026; zelfde gedrag als in Btechnics VTO door de scoped registry van HA).
// Daarom later opnieuw proberen tot de naam bekend is.
// Een oude kopie van de pagina (service worker) laadt soms eerst een oudere versie van dit script; een custom
// element kan niet opnieuw gedefinieerd worden. Dan de methodes van deze versie op de geregistreerde klasse
// zetten en de kaarten opnieuw opbouwen (zelfde oplossing als Btechnics VTO, 26/09/2026).
const vigiNewer = (a, b) => {
  const v = (x) => String(x || "0").split(".").map(Number);
  const [x, y] = [v(a), v(b)];
  const i = x.findIndex((n, k) => n !== (y[k] || 0));
  return i >= 0 && x[i] > (y[i] || 0);
};
const vigiCards = () => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.tagName === "BTECHNICS-VIGI-CARD") out.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return out;
};
// Met de scoped registry van HA hoort de kaart op de pagina niet altijd bij de klasse die customElements.get
// teruggeeft (vastgesteld 30/10/2026: kaart bleef 0.2.2, registry had 0.2.3). Daarom ook de klassen van de
// kaarten die echt op de pagina staan bijwerken.
const vigiUpgrade = (Cls) => {
  const cards = vigiCards();
  const classes = new Set([Cls, ...cards.map((c) => c.constructor)].filter((c) => c && c !== HTMLElement));
  let changed = false;
  for (const C of classes) {
    if (!vigiNewer(VIGI_VERSION, C.__vigiVer)) continue;
    for (const k of Object.getOwnPropertyNames(BtechnicsVigiCard.prototype)) {
      if (k === "constructor") continue;
      Object.defineProperty(C.prototype, k, Object.getOwnPropertyDescriptor(BtechnicsVigiCard.prototype, k));
    }
    C.__vigiVer = VIGI_VERSION;
    changed = true;
  }
  if (!changed) return;
  for (const el of cards) {
    if (!el._hass) continue;
    clearInterval(el._timer); el._timer = null;
    try { el._stopPlayer && el._stopPlayer(); el._init(); } catch (e) { /* volgende kaart */ }
  }
};
const vigiDefine = () => {
  const cur = customElements.get("btechnics-vigi-card");
  vigiUpgrade(cur);
  if (cur) return true;
  try {
    const R = class extends BtechnicsVigiCard {};
    R.__vigiVer = VIGI_VERSION;
    customElements.define("btechnics-vigi-card", R);
  } catch (e) { /* later opnieuw */ }
  return !!customElements.get("btechnics-vigi-card");
};
vigiDefine();
// De kaart verschijnt soms pas na 15 s of meer (trage start van de Pi, vastgesteld 30/10/2026): twee minuten
// lang elke 2 s nakijken, en bij elke paginawissel binnen HA.
let vigiTries = 0;
const vigiTick = setInterval(() => { vigiDefine(); if (++vigiTries >= 60) clearInterval(vigiTick); }, 2000);
window.addEventListener("load", vigiDefine);
window.addEventListener("location-changed", () => setTimeout(vigiDefine, 500));
window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "btechnics-vigi-card"))
  window.customCards.push({ type: "btechnics-vigi-card", name: "Btechnics VIGI", description: "Live raster en opnames van de VIGI recorder" });
