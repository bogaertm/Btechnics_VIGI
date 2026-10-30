/* Btechnics VIGI: dashboardkaart (live raster, opnames met kalender, zoombare tijdlijn, speler, download).
 * Enkel voor beheerders (backend weigert ook: alle opnamecommando's en adressen zijn admin-only).
 *
 * Afspelen (v0.3, metingen 30/10/2026): de recorder levert een opname altijd op normale snelheid en kent max 4
 * afspeelsessies. Daarom: meteen kijken via een stream (HA) en tegelijk het stuk als mp4 binnenhalen (3 delen
 * tegelijk). Zodra het bestand klaar is, schakelt de speler over: spoelen, 0,5x tot 16x, beeld per beeld, download.
 */
const VIGI_VERSION = "0.3.11";

// Oude video's echt loslaten: een verwijderde <video> met src blijft anders het bestand verder binnenhalen
// en neemt bandbreedte van het volgende venster (test 30/10/2026: volgend bestand pas na 90 s geladen).
function dropMedia(box) {
  box.querySelectorAll("ha-hls-player,video").forEach((x) => {
    if (x.tagName === "VIDEO") { try { x.pause(); x.removeAttribute("src"); x.load(); } catch (e) { /* niets */ } }
    x.remove();
  });
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");
const SPEEDS = [0.5, 1, 2, 4, 8, 16];
const MAAND = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"];
const DAG = ["ma", "di", "wo", "do", "vr", "za", "zo"];

function tzParts(ms, tz) {
  const f = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const o = {};
  for (const p of f.formatToParts(new Date(ms))) o[p.type] = p.value;
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour, mi: +o.minute, s: +o.second };
}
// lokale tijd (in tijdzone tz) naar epoch-seconden, ook rond zomer/wintertijd
function localToEpoch(y, m, d, h, mi, tz) {
  const want = Date.UTC(y, m - 1, d, h, mi, 0);
  let t = want;
  for (let i = 0; i < 3; i++) {
    const p = tzParts(t, tz);
    t -= Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) - want;
  }
  return Math.round(t / 1000);
}
const dayStart = (day, tz) => localToEpoch(+day.slice(0, 4), +day.slice(4, 6), +day.slice(6, 8), 0, 0, tz);
function addDays(day, n) {
  const t = new Date(Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8) + n));
  return `${t.getUTCFullYear()}${pad(t.getUTCMonth() + 1)}${pad(t.getUTCDate())}`;
}
function todayStr(tz) { const p = tzParts(Date.now(), tz); return `${p.y}${pad(p.mo)}${pad(p.d)}`; }
function hms(sec, tz) { const p = tzParts(sec * 1000, tz); return `${pad(p.h)}u${pad(p.mi)}m${pad(p.s)}`; }
function hm(sec, tz) { const p = tzParts(sec * 1000, tz); return `${pad(p.h)}u${pad(p.mi)}`; }
function dayLabel(day, long) {
  const t = new Date(Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8), 12));
  return t.toLocaleDateString("nl-BE", { weekday: long ? "long" : "short", day: "numeric", month: long ? "long" : "short", timeZone: "UTC" });
}
function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s >= 3600) return `${Math.floor(s / 3600)}u${pad(Math.floor((s % 3600) / 60))}`;
  if (s >= 60) return `${Math.floor(s / 60)} min${s % 60 ? " " + (s % 60) + " s" : ""}`;
  return `${s} s`;
}

class BtechnicsVigiCard extends HTMLElement {
  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }
  static getStubConfig() { return {}; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    else if (this._tab === "live") this._updateLive();
  }

  connectedCallback() { if (this._hass && this._ready) this._startTimers(); else if (this._hass && this._initAborted) { this._initAborted = false; this._init(); } }
  disconnectedCallback() { this._stopTimers(); this._stopAll(); }

  get _tz() { return (this._hass && this._hass.config && this._hass.config.time_zone) || Intl.DateTimeFormat().resolvedOptions().timeZone; }
  get _admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

  async _init() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._stopTimers();
    this._tab = this._tab || "live";
    const today = todayStr(this._tz);
    this._sel = this._sel || { channel: null, day: today, hour: null };
    this._cal = this._cal || { y: +today.slice(0, 4), m: +today.slice(4, 6) };
    this._days = this._days || {};
    this._speed = this._speed || 1;
    this._auto = this._auto !== false;
    this._shell();
    // Na een herstart van HA bleef de kaart op "Laden…" staan: de aanvraag ging verloren tijdens het opnieuw verbinden
    // of de koppeling was nog niet geladen (test 30/10/2026). Daarom met tijdslimiet en opnieuw proberen.
    const run = (this._initRun = (this._initRun || 0) + 1);
    for (let i = 0; ; i++) {
      try {
        const r = await Promise.race([this._hass.callWS({ type: "btechnics_vigi/channels" }),
          new Promise((_, no) => setTimeout(() => no(new Error("geen antwoord")), 8000))]);
        this._channels = r.channels || [];
        this._entry = r.entry_id;
        this._err = null;
        break;
      } catch (e) {
        if (run !== this._initRun) return;
        if (!this.isConnected) { this._initRun++; this._initAborted = true; return; }   // connectedCallback start opnieuw
        if (i >= 20) { this._err = "Recorder niet bereikbaar: " + (e.message || e.code || e); this._channels = []; break; }
        const b = this.shadowRoot.querySelector(".empty");
        if (b) b.textContent = "Laden… (Home Assistant start nog of de recorder antwoordt niet)";
        await new Promise((r) => setTimeout(r, 3000));
      }
    }
    if (this._sel.channel == null && this._channels.length) this._sel.channel = this._channels[0].channel;
    this._ready = true;
    this._shell();
    if (this.isConnected) this._startTimers();   // audit: geen timer als de kaart intussen weg is
  }

  _startTimers() {
    this._stopTimers();
    this._snapT = setInterval(() => this._refreshSnaps(), 4000);
    this._tickT = setInterval(() => this._tick(), 500);
  }
  _stopTimers() { clearInterval(this._snapT); clearInterval(this._tickT); this._snapT = this._tickT = null; }

  _shell() {
    const r = this.shadowRoot;
    const tabs = [["live", "Live"]].concat(this._admin ? [["rec", "Opnames"]] : []);
    r.innerHTML = `<style>${CSS}</style><ha-card>
      <div class="top"><div class="tabs">${tabs.map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
      <span class="ver">Btechnics VIGI ${VIGI_VERSION}</span></div>
      ${this._err ? `<div class="err">${esc(this._err)}</div>` : ""}
      <div id="body">${this._ready ? "" : `<div class="empty">Laden…</div>`}</div></ha-card>`;
    r.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
      if (this._tab === b.dataset.tab) return;
      this._tab = b.dataset.tab;
      this._stopAll();
      this._shell();
    }));
    if (!this._ready) return;
    if (this._tab === "rec" && this._admin) this._renderRec(); else this._renderLive();
  }

  _chName(ch) { const c = (this._channels || []).find((x) => x.channel === ch); return c ? c.name : `Kanaal ${ch}`; }

  /* ================= Live ================= */
  _renderLive() {
    const body = this.shadowRoot.getElementById("body");
    const chs = this._channels || [];
    if (!this._admin) { body.innerHTML = `<div class="empty">Live beeld is enkel voor beheerders.</div>`; return; }
    if (!chs.length) { body.innerHTML = `<div class="empty">Geen camera's gevonden.</div>`; return; }
    body.innerHTML = `<div class="grid">${chs.map((c) => `
      <button class="tile ${c.online ? "" : "unavail"}" data-ch="${c.channel}">
        <div class="img"><img alt=""><span class="off">${c.online ? "Laden…" : "Niet bereikbaar"}</span></div>
        <div class="cap"><span class="dot"></span><b>${esc(c.name)}</b><span class="ch">kanaal ${c.channel}</span></div>
      </button>`).join("")}</div>
      <div class="hint">Beelden vernieuwen om de 4 seconden zolang ze zichtbaar zijn. Klik op een camera voor live beeld.</div>
      <div class="modal" id="modal" hidden><div class="mbox"><div class="mhead"><b id="mtitle"></b><button class="x" id="mclose" title="Sluiten">✕</button></div><div class="vbox" id="mlive"></div></div></div>`;
    body.querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => this._openLive(+t.dataset.ch)));
    body.querySelector("#mclose").addEventListener("click", () => this._closeLive());
    body.querySelector("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") this._closeLive(); });
    // enkel zichtbare beelden vernieuwen (audit: belasting Pi en recorder)
    this._io && this._io.disconnect();
    this._visible = new Set();
    this._io = new IntersectionObserver((ents) => {
      for (const e of ents) { const ch = +e.target.dataset.ch; if (e.isIntersecting) this._visible.add(ch); else this._visible.delete(ch); }
    });
    body.querySelectorAll(".tile").forEach((t) => this._io.observe(t));
    setTimeout(() => this._refreshSnaps(true), 50);
  }

  _updateLive() { /* status komt uit channels; elke minuut opnieuw bij hertekenen */ }

  async _refreshSnaps(all) {
    if (this._tab !== "live" || !this.shadowRoot || document.hidden || this._liveCh != null || !this._entry) return;
    for (const t of this.shadowRoot.querySelectorAll(".tile")) {
      const ch = +t.dataset.ch;
      if (t.classList.contains("unavail") || t._busy || (!all && !(this._visible && this._visible.has(ch)))) continue;
      t._busy = true;
      this._hass.fetchWithAuth(`/api/btechnics_vigi/snapshot/${this._entry}/${ch}`).then(async (r) => {
        if (!r.ok) throw new Error(r.status);
        const url = URL.createObjectURL(await r.blob());
        const img = t.querySelector("img");
        if (img._url) URL.revokeObjectURL(img._url);
        img._url = url;
        img.src = url;
        t.querySelector(".off").textContent = "";
      }).catch(() => { t.querySelector(".off").textContent = "Geen beeld"; }).finally(() => { t._busy = false; });
    }
  }

  async _openLive(ch) {
    this._liveCh = ch;
    const m = this.shadowRoot.getElementById("modal");
    this.shadowRoot.getElementById("mtitle").textContent = this._chName(ch) + " live";
    const host = this.shadowRoot.getElementById("mlive");
    host.innerHTML = `<div class="status">Live beeld starten…</div>`;
    m.hidden = false;
    // 1) WebRTC via go2rtc: meteen beeld. 2) terugval HLS (start pas na 12 s of meer, vastgesteld 30/10/2026)
    if (await this._webrtc(ch, host)) return;
    if (this._liveCh !== ch) return;
    try {
      const { url } = await this._hass.callWS({ type: "btechnics_vigi/live", channel: ch });
      const ok = await this._waitHls(url, () => this._liveCh === ch,
        (s) => { const st = host.querySelector(".status"); if (st) st.textContent = `Live beeld starten via HLS… ${s} s (tot 15 s)`; });
      if (this._liveCh !== ch) return;
      if (!ok) throw new Error("de camera antwoordt niet");
      host.innerHTML = "";
      host.appendChild(this._hlsPlayer(url));
    } catch (e) {
      host.innerHTML = `<div class="status err">Live beeld mislukt: ${esc(e.message || e.code)}</div>`;
    }
  }

  async _webrtc(ch, host) {
    let pc;
    try {
      const { ice_servers } = await this._hass.callWS({ type: "btechnics_vigi/ice" });
      pc = new RTCPeerConnection({ iceServers: ice_servers });
      this._pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      const video = document.createElement("video");
      video.muted = true; video.autoplay = true; video.playsInline = true; video.controls = true;
      pc.ontrack = (ev) => { video.srcObject = ev.streams[0] || new MediaStream([ev.track]); };
      await pc.setLocalDescription(await pc.createOffer());
      // zonder trickle: wachten tot de kandidaten verzameld zijn (max 2 s)
      await new Promise((res) => {
        if (pc.iceGatheringState === "complete") return res();
        const t = setTimeout(res, 2000);
        pc.addEventListener("icegatheringstatechange", () => { if (pc.iceGatheringState === "complete") { clearTimeout(t); res(); } });
      });
      if (this._liveCh !== ch) { pc.close(); return true; }
      const { answer } = await this._hass.callWS({ type: "btechnics_vigi/webrtc", channel: ch, offer: pc.localDescription.sdp });
      await pc.setRemoteDescription({ type: "answer", sdp: answer });
      const ok = await new Promise((res) => {
        const t = setTimeout(() => res(false), 8000);
        video.addEventListener("playing", () => { clearTimeout(t); res(true); }, { once: true });
        pc.addEventListener("connectionstatechange", () => { if (pc.connectionState === "failed") { clearTimeout(t); res(false); } });
        video.play().catch(() => {});
        host.innerHTML = "";
        host.appendChild(video);
        const st = document.createElement("div"); st.className = "status"; st.textContent = "Verbinden…"; host.appendChild(st);
        video.addEventListener("playing", () => st.remove(), { once: true });
      });
      if (!ok) { pc.close(); this._pc = null; host.innerHTML = `<div class="status">WebRTC lukt niet hier, live beeld via HLS…</div>`; }
      return ok;
    } catch (e) {
      if (pc) pc.close();
      this._pc = null;
      return false;
    }
  }

  _closeLive() {
    this._liveCh = null;
    if (this._pc) { try { this._pc.close(); } catch (e) { /* al dicht */ } this._pc = null; }
    const m = this.shadowRoot && this.shadowRoot.getElementById("modal");
    if (m) { m.hidden = true; this.shadowRoot.getElementById("mlive").innerHTML = ""; }
  }

  // HA geeft de playlist pas als er 2 segmenten zijn (live: vaak meer dan 12 s, vastgesteld 30/10/2026);
  // ha-hls-player geeft bij een 404 meteen op. Daarom eerst wachten tot het adres antwoordt.
  async _waitHls(url, isCurrent, onWait) {
    const abs = this._hass.hassUrl ? this._hass.hassUrl(url) : new URL(url, location.origin).href;
    const t0 = Date.now();
    while (Date.now() - t0 < 45000) {
      if (!isCurrent()) return false;
      onWait && onWait(Math.round((Date.now() - t0) / 1000));
      const tk = setInterval(() => onWait && isCurrent() && onWait(Math.round((Date.now() - t0) / 1000)), 1000);
      const ac = new AbortController();
      const to = setTimeout(() => { ac.abort(); }, 20000);
      try {
        const r = await fetch(abs, { signal: ac.signal, cache: "no-store" });
        if (r.ok) return true;
      } catch (e) { /* opnieuw */ } finally { clearTimeout(to); clearInterval(tk); }
      await new Promise((res) => setTimeout(res, 1000));
    }
    return false;
  }

  _hlsPlayer(url) {
    const p = document.createElement("ha-hls-player");
    p.hass = this._hass;
    // ha-hls-player bouwt adressen met new URL(..., this.url): een relatief adres geeft "Invalid base URL" (30/10/2026)
    p.url = this._hass.hassUrl ? this._hass.hassUrl(url) : new URL(url, location.origin).href;
    p.controls = true; p.muted = true; p.autoPlay = true; p.playsInline = true; p.allowExoPlayer = true;
    return p;
  }

  /* ================= Opnames ================= */
  _renderRec() {
    const body = this.shadowRoot.getElementById("body");
    const chs = this._channels || [];
    body.innerHTML = `
      <div class="rec">
        <aside class="side">
          <div class="chips" role="tablist">${chs.map((c) => `<button class="chip ${c.channel === this._sel.channel ? "on" : ""}" data-ch="${c.channel}">${esc(c.name)}</button>`).join("")}</div>
          <div class="cal" id="cal"></div>
          <div class="sub" id="range"></div>
        </aside>
        <section class="main">
          <div class="dayhead"><b id="dtitle"></b><span class="sub" id="dinfo"></span></div>
          <div class="hours" id="hours"></div>
          <div class="tlhead"><span id="tltitle" class="sub"></span><span class="tlnav" id="tlnav"></span></div>
          <div class="tlwrap"><div class="tlbar" id="tlbar"><div class="tip" id="tip" hidden></div></div><div class="ticks" id="ticks"></div></div>
          <div class="pieces" id="pieces"></div>
          <div class="player" id="player" hidden>
            <div class="phead"><b id="pclock"></b><span class="sub" id="pname"></span></div>
            <div class="vbox" id="vbox"><div class="status" id="pstatus"></div><div class="prog" id="prog" hidden><i></i></div></div>
            <div class="ctrl">
              <button id="prev" title="Vorig stuk">⏮</button><button id="b10" title="10 s terug">−10 s</button>
              <button id="pp" title="Afspelen of pauze (spatie)">⏯</button><button id="f10" title="10 s vooruit">+10 s</button>
              <button id="next" title="Volgend stuk">⏭</button>
              <span class="sep"></span>
              <span class="speeds">${SPEEDS.map((s) => `<button class="sp ${s === this._speed ? "on" : ""}" data-s="${s}">${s}x</button>`).join("")}</span>
              <span class="sep"></span>
              <button id="fb" title="Beeld terug (pauze)">◀︎ beeld</button><button id="ff" title="Beeld vooruit (pauze)">beeld ▶︎</button>
              <span class="grow"></span>
              <label class="auto"><input type="checkbox" id="auto" ${this._auto ? "checked" : ""}> Automatisch verder</label>
              <button id="dl" class="dl" disabled title="Download als mp4 (komt in het logboek)">⬇ Download</button>
              <button id="close" title="Speler sluiten">Sluiten</button>
            </div>
            <div class="sub" id="pnote"></div>
          </div>
        </section>
      </div>`;
    body.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
      this._sel.channel = +b.dataset.ch; this._stopAll(); this._renderRec();
    }));
    const tl = body.querySelector("#tlbar");
    tl.addEventListener("click", (ev) => this._clickTimeline(ev));
    tl.addEventListener("mousemove", (ev) => this._hoverTimeline(ev));
    tl.addEventListener("mouseleave", () => { this.shadowRoot.getElementById("tip").hidden = true; });
    const $ = (id) => body.querySelector("#" + id);
    $("prev").addEventListener("click", () => this._jumpSeg(-1));
    $("next").addEventListener("click", () => this._jumpSeg(1));
    $("b10").addEventListener("click", () => this._seekBy(-10));
    $("f10").addEventListener("click", () => this._seekBy(10));
    $("pp").addEventListener("click", () => this._togglePlay());
    $("fb").addEventListener("click", () => this._frame(-1));
    $("ff").addEventListener("click", () => this._frame(1));
    $("close").addEventListener("click", () => this._stopAll(true));
    $("dl").addEventListener("click", () => this._download());
    $("auto").addEventListener("change", (e) => { this._auto = e.target.checked; });
    body.querySelectorAll(".sp").forEach((b) => b.addEventListener("click", () => this._setSpeed(+b.dataset.s)));
    if (!this._keys) {
      this._keys = (ev) => {
        if (this._tab !== "rec" || !this._cur || ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) return;
        const path = ev.composedPath ? ev.composedPath() : [];
        if (!path.includes(this)) return;
        if (ev.key === " ") { ev.preventDefault(); this._togglePlay(); }
        else if (ev.key === "ArrowLeft") this._seekBy(-10);
        else if (ev.key === "ArrowRight") this._seekBy(10);
        else if (ev.key === ",") this._frame(-1);
        else if (ev.key === ".") this._frame(1);
      };
      this.addEventListener("keydown", this._keys);
      this.tabIndex = 0;
    }
    this._drawCalendar();
    this._loadDays();
    this._loadDay();
  }

  /* ---- kalender met puntjes (vraag Matthias 30/10/2026) ---- */
  _drawCalendar() {
    const el = this.shadowRoot.getElementById("cal");
    if (!el) return;
    const { y, m } = this._cal;
    const days = this._days[this._sel.channel] || null;
    const today = todayStr(this._tz);
    const first = new Date(Date.UTC(y, m - 1, 1));
    const lead = (first.getUTCDay() + 6) % 7;
    const n = new Date(Date.UTC(y, m, 0)).getUTCDate();
    let cells = "";
    for (let i = 0; i < lead; i++) cells += `<span></span>`;
    for (let d = 1; d <= n; d++) {
      const key = `${y}${pad(m)}${pad(d)}`;
      const has = days && days.has(key);
      const fut = key > today;
      cells += `<button class="cd ${has ? "has" : ""} ${key === this._sel.day ? "sel" : ""} ${key === today ? "today" : ""}" data-d="${key}" ${fut ? "disabled" : ""}>${d}${has ? "<i></i>" : ""}</button>`;
    }
    const canNext = `${y}${pad(m)}` < today.slice(0, 6);
    el.innerHTML = `<div class="calhead"><button class="nav" id="cprev" title="Vorige maand">‹</button><b>${MAAND[m - 1]} ${y}</b>
      <button class="nav" id="cnext" title="Volgende maand" ${canNext ? "" : "disabled"}>›</button></div>
      <div class="calgrid">${DAG.map((d) => `<span class="dn">${d}</span>`).join("")}${cells}</div>
      <div class="calfoot"><span><i class="lg"></i> opnames</span><button class="link" id="ctoday">Vandaag</button></div>`;
    el.querySelectorAll(".cd").forEach((b) => b.addEventListener("click", () => {
      this._sel.day = b.dataset.d; this._sel.hour = null; this._stopAll(); this._drawCalendar(); this._loadDay();
    }));
    el.querySelector("#cprev").addEventListener("click", () => this._shiftMonth(-1));
    el.querySelector("#cnext").addEventListener("click", () => this._shiftMonth(1));
    el.querySelector("#ctoday").addEventListener("click", () => {
      const t = todayStr(this._tz);
      this._sel.day = t; this._sel.hour = null; this._cal = { y: +t.slice(0, 4), m: +t.slice(4, 6) };
      this._stopAll(); this._drawCalendar(); this._loadDays(); this._loadDay();
    });
  }

  _shiftMonth(n) {
    let { y, m } = this._cal;
    m += n; if (m < 1) { m = 12; y--; } if (m > 12) { m = 1; y++; }
    this._cal = { y, m };
    this._drawCalendar();
    this._loadDays();
  }

  async _loadDays() {
    const ch = this._sel.channel;
    if (ch == null) return;
    const { y, m } = this._cal;
    const key = `${ch}:${y}${pad(m)}`;
    this._daysDone = this._daysDone || new Set();
    if (this._daysDone.has(key)) { this._drawRange(); return; }
    const req = (this._daysReq = (this._daysReq || 0) + 1);
    // deze maand en 2 maanden ervoor in een aanvraag (de recorder bewaart ca. 6 weken)
    let sy = y, sm = m - 2; if (sm < 1) { sm += 12; sy--; }
    try {
      const r = await this._hass.callWS({ type: "btechnics_vigi/record/days", channel: ch, start: `${sy}${pad(sm)}`, end: `${y}${pad(m)}` });
      if (req !== this._daysReq || ch !== this._sel.channel) return;   // audit: antwoord van een vorig kanaal negeren
      const set = this._days[ch] || (this._days[ch] = new Set());
      r.days.forEach((d) => set.add(d));
      for (let i = 0; i < 3; i++) { let mm = m - i, yy = y; if (mm < 1) { mm += 12; yy--; } this._daysDone.add(`${ch}:${yy}${pad(mm)}`); }
      this._drawCalendar();
      this._drawRange();
    } catch (e) {
      const el = this.shadowRoot.getElementById("range");
      if (el) el.textContent = "Dagen met opnames ophalen mislukt: " + (e.message || e.code);
    }
  }

  _drawRange() {
    const el = this.shadowRoot.getElementById("range");
    const s = [...(this._days[this._sel.channel] || [])].sort();
    if (!el) return;
    el.textContent = s.length ? `Opnames van ${dayLabel(s[0])} tot ${dayLabel(s[s.length - 1])}` : "Nog geen dagen met opnames gevonden.";
  }

  /* ---- dag: uren en tijdlijn ---- */
  async _loadDay() {
    const ch = this._sel.channel, day = this._sel.day;
    const req = (this._segReq = (this._segReq || 0) + 1);
    this._segs = [];
    this._drawDay("Opnames zoeken…");
    if (ch == null) return;
    try {
      const r = await this._hass.callWS({ type: "btechnics_vigi/record/search", channel: ch, day });
      if (req !== this._segReq) return;
      this._segs = r.segments || [];
      this._drawDay();
    } catch (e) {
      if (req === this._segReq) this._drawDay("Zoeken mislukt: " + (e.message || e.code));
    }
  }

  _range() {
    const d0 = dayStart(this._sel.day, this._tz), d1 = dayStart(addDays(this._sel.day, 1), this._tz);
    if (this._sel.hour == null) return [d0, d1];
    const a = d0 + this._hourOffsets()[this._sel.hour];
    return [a, Math.min(a + 3600, d1)];
  }
  // begin van elk uur ten opzichte van middernacht, ook op een dag van 23 of 25 uur (audit: zomer/wintertijd)
  _hourOffsets() {
    const d0 = dayStart(this._sel.day, this._tz), d1 = dayStart(addDays(this._sel.day, 1), this._tz);
    const out = [];
    for (let t = d0; t < d1; t += 3600) out.push(t - d0);
    return out;
  }

  _drawDay(msg) {
    const $ = (id) => this.shadowRoot.getElementById(id);
    if (!$("dtitle")) return;
    const segs = this._segs || [];
    $("dtitle").textContent = `${this._chName(this._sel.channel)} · ${dayLabel(this._sel.day, true)}`;
    const tot = segs.reduce((a, s) => a + (s.end - s.start), 0);
    $("dinfo").textContent = msg || (segs.length ? `${segs.length} ${segs.length === 1 ? "stuk" : "stukken"}, samen ${dur(tot)} opgenomen` : "Geen opnames op deze dag.");
    // uren: hoeveel van elk uur is opgenomen
    const d0 = dayStart(this._sel.day, this._tz);
    const offs = this._hourOffsets();
    const hp = (t) => tzParts(t * 1000, this._tz).h;
    $("hours").innerHTML = offs.map((o, i) => {
      const a = d0 + o, b = a + 3600;
      let rec = 0;
      for (const s of segs) rec += Math.max(0, Math.min(s.end, b) - Math.max(s.start, a));
      const f = Math.min(1, rec / 3600);
      return `<button class="hr ${this._sel.hour === i ? "sel" : ""} ${rec ? "has" : ""}" data-i="${i}" title="${pad(hp(a))}u: ${rec ? dur(rec) + " opgenomen" : "geen opnames"}"><span style="--f:${rec ? Math.max(0.12, f) : 0}"></span><em>${pad(hp(a))}</em></button>`;
    }).join("");
    this.shadowRoot.querySelectorAll(".hr").forEach((b) => b.addEventListener("click", () => {
      const i = +b.dataset.i; this._sel.hour = this._sel.hour === i ? null : i; this._drawDay();
    }));
    this._drawTimeline();
  }

  _drawTimeline() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const bar = $("tlbar");
    if (!bar) return;
    const [a, b] = this._range(), L = b - a;
    const zoom = this._sel.hour != null;
    $("tltitle").textContent = zoom ? `Uur ${hm(a, this._tz)} tot ${hm(b, this._tz)}` : "Hele dag (klik op een uur hierboven om in te zoomen)";
    $("tlnav").innerHTML = zoom ? `<button class="nav" id="hprev">‹ uur</button><button class="nav" id="hall">Hele dag</button><button class="nav" id="hnext">uur ›</button>` : "";
    if (zoom) {
      const n = this._hourOffsets().length;
      $("hprev").addEventListener("click", () => { if (this._sel.hour > 0) { this._sel.hour--; this._drawDay(); } });
      $("hnext").addEventListener("click", () => { if (this._sel.hour < n - 1) { this._sel.hour++; this._drawDay(); } });
      $("hall").addEventListener("click", () => { this._sel.hour = null; this._drawDay(); });
    }
    const segs = (this._segs || []).filter((s) => s.end > a && s.start < b);
    const minW = zoom ? 0.6 : 0.25;
    const tip = $("tip");
    bar.innerHTML = segs.map((s) => {
      const l = ((Math.max(s.start, a) - a) / L) * 100, w = ((Math.min(s.end, b) - Math.max(s.start, a)) / L) * 100;
      return `<i style="left:${l}%;width:${Math.max(minW, w)}%"></i>`;
    }).join("") + `<u id="now"></u><em id="pos" hidden></em>`;
    bar.appendChild(tip);
    // tijdsaanduiding onder de balk, correct rond zomer/wintertijd
    const ticks = [];
    if (zoom) { for (let mi = 0; mi <= 60; mi += 10) ticks.push([a + mi * 60, mi === 60 ? hm(b, this._tz) : hm(a + mi * 60, this._tz)]); }
    else { const offs = this._hourOffsets(); offs.forEach((o, i) => { if (i % 3 === 0) ticks.push([a + o, `${pad(tzParts((a + o) * 1000, this._tz).h)}u`]); }); ticks.push([b, "24u"]); }
    $("ticks").innerHTML = ticks.map(([t, l]) => `<span style="left:${((t - a) / L) * 100}%">${l}</span>`).join("");
    // in een uur: de stukken als lijst (overzichtelijk, enkel dat uur)
    const list = $("pieces");
    list.innerHTML = zoom
      ? (segs.length ? segs.map((s, i) => `<button class="pc" data-t="${Math.max(s.start, a)}">▶ ${hms(Math.max(s.start, a), this._tz)} <span class="sub">${dur(Math.min(s.end, b) - Math.max(s.start, a))}</span></button>`).join("") : `<span class="sub">Geen opnames in dit uur.</span>`)
      : "";
    list.querySelectorAll(".pc").forEach((p) => p.addEventListener("click", () => this._play(+p.dataset.t)));
    this._tick();
  }

  _timeAt(ev) {
    const bar = this.shadowRoot.getElementById("tlbar"), r = bar.getBoundingClientRect();
    const [a, b] = this._range();
    return Math.floor(a + Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)) * (b - a));
  }

  _hoverTimeline(ev) {
    const tip = this.shadowRoot.getElementById("tip"), bar = this.shadowRoot.getElementById("tlbar");
    const t = this._timeAt(ev);
    const seg = (this._segs || []).find((s) => t >= s.start && t < s.end);
    tip.textContent = hms(t, this._tz) + (seg ? "" : " (geen opname)");
    tip.style.left = `${Math.min(bar.clientWidth - 90, Math.max(0, ev.clientX - bar.getBoundingClientRect().left - 45))}px`;
    tip.hidden = false;
  }

  async _clickTimeline(ev, again) {
    let t = this._timeAt(ev);
    const segs = this._segs || [];
    if (!segs.find((s) => t >= s.start && t < s.end)) {
      const n = segs.find((s) => s.start > t);
      if (!n) {
        // vandaag na het laatste gekende stuk: eerst opnieuw zoeken, intussen kan er meer opgenomen zijn
        if (!again && this._sel.day === todayStr(this._tz)) { await this._refreshSegs(); return this._clickTimeline(ev, true); }
        this.shadowRoot.getElementById("dinfo").textContent = "Na dit tijdstip is er niets opgenomen op deze dag.";
        return;
      }
      t = n.start;
    }
    this._play(t);
  }

  /* ---- speler ---- */
  _segAt(t) { return (this._segs || []).find((s) => t >= s.start && t < s.end); }

  async _play(t, endHint, cont) {
    t = Math.floor(t);
    const segs = this._segs || [];
    const seg = this._segAt(t) || segs.find((s) => s.start > t);
    if (!seg) return;
    if (t < seg.start) t = seg.start;
    // eerste venster kort (3 min, binnen na ca. 1 min), daarna 5 min vooraf binnenhalen (zie _prefetchNext).
    // Gemeten 30/10/2026: binnenhalen loopt ca. 2,4x sneller dan echte tijd, 5 min is dus binnen na ca. 2 min.
    const end = endHint || Math.min(seg.end, t + 180);
    // cont (automatisch verder): geen stream starten. Die nam een plaats van de recorder in (max 4), vertraagde het
    // binnenhalen en bleef hangen (test 30/10/2026). Het laatste beeld blijft staan tot het volgende venster binnen is.
    if (cont) { const old = this._cur && this._cur.video; if (old) old.pause(); this._req = (this._req || 0) + 1; this._stopStream(); }
    else this._stopAll(false);
    const req = (this._req = (this._req || 0) + 1);
    this._cur = { req, channel: this._sel.channel, start: t, end, seg, mode: "stream", clip: null, startedAt: Date.now(), cont: !!cont };
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("player").hidden = false;
    $("pname").textContent = `${this._chName(this._sel.channel)} · ${dayLabel(this._sel.day)} · venster ${hms(t, this._tz)} tot ${hms(end, this._tz)}`;
    this._setStatus(cont ? null : "Verbinden met de recorder…", cont ? "Volgend stuk binnenhalen…" : null);
    this._setControls();
    $("player").scrollIntoView({ block: "nearest", behavior: "smooth" });
    // 1) meteen kijken (stream)  2) tegelijk binnenhalen (bestand)
    const parts = (seg.parts || [[seg.start, seg.end]]).filter((p) => p[1] > t && p[0] < end);
    const clipP = this._hass.callWS({ type: "btechnics_vigi/clip/start", channel: this._sel.channel, start: t, end, parts }).catch((e) => ({ state: "error", error: e.message || e.code }));
    const streamP = cont ? null : this._hass.callWS({ type: "btechnics_vigi/replay", channel: this._sel.channel, start: t, end }).catch((e) => ({ error: e.message || e.code }));
    // review: een stream die niet meer nodig is meteen stoppen (anders bleef tot 60 s een recorderplaats bezet)
    const dropStream = () => { if (streamP) streamP.then((st) => { if (st && st.url) this._hass.callWS({ type: "btechnics_vigi/replay/stop", url: st.url }).catch(() => {}); }); };
    const clip = await clipP;
    if (req !== this._req) { dropStream(); return; }
    this._cur.clip = clip;
    // de server kort het einde in (max 15 min, niet later dan nu min 5 s): verder rekenen met dat einde (review)
    if (clip.end && clip.end < this._cur.end) this._cur.end = clip.end;
    if (clip.state === "ready") { dropStream(); this._toFile(clip, t); return; }
    if (cont) { this._pollClip(req); return; }
    const st = await streamP;
    if (req !== this._req) { dropStream(); return; }
    if (st.url && this._cur.mode === "stream") {
      this._cur.streamUrl = st.url;
      const ok = await this._waitHls(st.url, () => req === this._req && this._cur && this._cur.mode === "stream",
        (s) => this._setStatus(`Verbinden met de recorder… ${s} s`));
      if (!ok || req !== this._req || this._cur.mode !== "stream") return;
      const vb = $("vbox");
      dropMedia(vb);
      vb.insertBefore(this._hlsPlayer(st.url), vb.firstChild);
      this._cur.streamStart = Date.now();
      this._setStatus(null);
    }
    this._pollClip(req);
  }

  async _pollClip(req) {
    while (req === this._req && this._cur && this._cur.clip && this._cur.clip.clip_id && ["fetching", "joining"].includes(this._cur.clip.state)) {
      const c = this._cur.clip;
      const pct = Math.round((c.progress || 0) * 100);
      const cont = this._cur.cont;
      this._setStatus(this._cur.streamStart || cont ? null : "Verbinden met de recorder…",
        c.state === "joining" ? "Samenvoegen…" : cont ? `Volgend stuk binnenhalen ${pct} %, het speelt daarna vanzelf verder`
          : `Binnenhalen ${pct} % · spoelen, versnellen en downloaden zodra het binnen is`, c.progress);
      await new Promise((r) => setTimeout(r, 1000));
      if (req !== this._req) return;
      try { this._cur.clip = await this._hass.callWS({ type: "btechnics_vigi/clip/status", clip_id: c.clip_id }); }
      catch (e) { this._cur.clip = { state: "error", error: e.message || e.code }; }
    }
    if (req !== this._req || !this._cur) return;
    const c = this._cur.clip;
    if (c && c.state === "ready") {
      // verder op de plaats waar de stream nu is
      const at = this._cur.streamStart ? Math.min(this._cur.end - 1, this._cur.start + (Date.now() - this._cur.streamStart) / 1000) : this._cur.start;
      this._toFile(c, at);
    } else {
      this._setStatus(null, `Binnenhalen mislukt: ${esc((c && c.error) || "onbekend")}. Kijken kan wel, spoelen en downloaden niet.`);
    }
  }

  async _toFile(clip, clock) {
    const req = this._req;
    let signed;
    // korte geldigheid (review: met een adres van 4 uur kon het bestand buiten het logboek om gekopieerd worden);
    // verloopt het tijdens het kijken, dan vraagt de error-handler hieronder een nieuw adres
    try { signed = (await this._hass.callWS({ type: "auth/sign_path", path: `/api/btechnics_vigi/clip/${clip.clip_id}`, expires: 1800 })).path; }
    catch (e) { this._setStatus(null, "Afspelen mislukt: " + (e.message || e.code)); return; }
    if (req !== this._req) return;
    this._stopStream();
    const vb = this.shadowRoot.getElementById("vbox");
    dropMedia(vb);
    const v = document.createElement("video");
    v.controls = true; v.muted = true; v.playsInline = true; v.preload = "auto";
    // geen eigen downloadknop van de browser: downloaden enkel via de knop Download (komt in het logboek)
    v.setAttribute("controlslist", "nodownload noplaybackrate"); v.disablePictureInPicture = true;
    v.addEventListener("contextmenu", (e) => e.preventDefault());
    v.src = signed;
    vb.insertBefore(v, vb.firstChild);
    Object.assign(this._cur, { mode: "file", clip, video: v });
    v.addEventListener("loadedmetadata", () => {
      v._ok = true;
      v.currentTime = Math.max(0, this._clockToMedia(clock));
      v.defaultPlaybackRate = this._speed;
      v.playbackRate = this._speed;
      v.play().catch(() => {});
    }, { once: true });
    // enkel de speler die nu speelt: een oude video meldt bij het loslaten (load) opnieuw 1x
    v.addEventListener("ratechange", () => { if (this._cur && this._cur.video === v && v.readyState > 0 && Math.abs(v.playbackRate - this._speed) > 0.01) { this._speed = v.playbackRate; this._markSpeed(); } });
    v.addEventListener("ended", () => { if (this._cur && this._cur.video === v) this._onEnded(); });
    v.addEventListener("error", () => {
      if (!this._cur || this._cur.video !== v) return;
      const code = v.error ? v.error.code : 0;
      // al eens geladen: adres verlopen of verbinding weg, opnieuw ondertekenen en verder op dezelfde plaats
      if (v._ok && (!this._resign || Date.now() - this._resign > 20000)) {
        this._resign = Date.now();
        this._toFile(this._cur.clip, this._clockNow());
        return;
      }
      this._setStatus(null, code === 4 ? "Deze browser kan het bestand niet afspelen (H.265). Probeer Chrome, Safari of de HA-app, of download het."
        : `Afspelen mislukt (fout ${code}). Kies het tijdstip opnieuw op de tijdlijn.`);
    });
    this._setStatus(null, "Binnen: spoelen met de balk, snelheid kiezen, beeld per beeld bij pauze.");
    this._setControls();
    this._prefetchNext();
  }

  _clockToMedia(clock) {
    const map = (this._cur && this._cur.clip && this._cur.clip.map) || [];
    let off = 0;
    for (const p of map) {
      if (clock < p.clock) return off;
      if (clock < p.clock + p.len) return off + (clock - p.clock);
      off += p.len;
    }
    return off;
  }
  _mediaToClock(m) {
    const map = (this._cur && this._cur.clip && this._cur.clip.map) || [];
    let off = 0;
    for (const p of map) {
      if (m < off + p.len) return p.clock + (m - off);
      off += p.len;
    }
    const last = map[map.length - 1];
    return last ? last.clock + last.len : this._cur.start;
  }
  _clockNow() {
    const c = this._cur;
    if (!c) return null;
    if (c.mode === "file" && c.video) return this._mediaToClock(c.video.currentTime);
    if (c.streamStart) return Math.min(c.end, c.start + (Date.now() - c.streamStart) / 1000);
    return c.start;
  }

  _tick() {
    if (!this.shadowRoot) return;
    const now = this.shadowRoot.getElementById("now");
    if (now) {
      const [a, b] = this._range(), t = Date.now() / 1000;
      now.hidden = !(t > a && t < b);
      if (!now.hidden) now.style.left = `${((t - a) / (b - a)) * 100}%`;
    }
    const pos = this.shadowRoot.getElementById("pos");
    const clock = this._clockNow();
    if (pos) {
      const [a, b] = this._range();
      pos.hidden = clock == null || clock < a || clock > b;
      if (!pos.hidden) pos.style.left = `${((clock - a) / (b - a)) * 100}%`;
    }
    const pc = this.shadowRoot.getElementById("pclock");
    if (pc && clock != null) pc.textContent = hms(Math.floor(clock), this._tz);
  }

  _setStatus(big, small, progress) {
    const $ = (id) => this.shadowRoot && this.shadowRoot.getElementById(id);
    if (!$("pstatus")) return;
    $("pstatus").textContent = big || "";
    $("pstatus").hidden = !big;
    if (small != null) $("pnote").textContent = small;
    const pr = $("prog");
    pr.hidden = progress == null;
    if (progress != null) pr.firstChild.style.width = `${Math.round(progress * 100)}%`;
  }

  _setControls() {
    const file = this._cur && this._cur.mode === "file";
    const $ = (id) => this.shadowRoot.getElementById(id);
    ["b10", "f10", "pp", "fb", "ff", "dl"].forEach((id) => { if ($(id)) $(id).disabled = !file; });
    this.shadowRoot.querySelectorAll(".sp").forEach((b) => { b.disabled = !file; });
    this._markSpeed();
  }
  _markSpeed() { this.shadowRoot.querySelectorAll(".sp").forEach((b) => b.classList.toggle("on", +b.dataset.s === this._speed)); }

  _setSpeed(s) {
    this._speed = s;
    const v = this._cur && this._cur.video;
    if (v) v.playbackRate = s;
    this._markSpeed();
    if (s >= 4 && this._cur && this._cur.mode === "file") this._prefetchNext();
  }
  _togglePlay() { const v = this._cur && this._cur.video; if (v) { if (v.paused) v.play().catch(() => {}); else v.pause(); } }
  _seekBy(d) {
    const v = this._cur && this._cur.video;
    if (!v) return;
    const target = v.currentTime + d;
    if (target >= (v.duration || 0) - 0.1 && d > 0) { this._onEnded(); return; }
    v.currentTime = Math.max(0, target);
  }
  _frame(n) {
    const v = this._cur && this._cur.video;
    if (!v) return;
    v.pause();
    v.currentTime = Math.max(0, v.currentTime + n / 20);   // ca. 1 beeld (20 tot 25 bps)
  }

  _jumpSeg(dir) {
    const clock = this._clockNow();
    const segs = this._segs || [];
    if (clock == null) return;
    if (dir > 0) {
      const n = segs.find((s) => s.start > clock + 1);
      if (n) this._play(n.start);
    } else {
      // meer dan 5 s in een stuk: terug naar het begin ervan; anders het vorige stuk
      const cur = this._segAt(clock);
      if (cur && clock - cur.start > 5) { this._play(cur.start); return; }
      const before = cur ? cur.start : clock;
      const prev = [...segs].reverse().find((s) => s.start < before - 1);
      if (prev) this._play(prev.start);
    }
  }

  async _onEnded() {
    if (!this._cur || !this._auto) return;
    // audit: venster stopte na 1 uur of sloeg de rest van een lang stuk over: verder met het volgende venster
    let w = this._nextWindow();
    // vandaag: de lijst van stukken is van bij het openen. Eerst opnieuw zoeken, er kan intussen meer opgenomen zijn
    // (test 30/10/2026: "Einde" terwijl de camera verder opnam).
    if (!w && this._sel.day === todayStr(this._tz)) {
      const req = this._req, at = this._cur.end;
      this._setStatus(null, "Nieuwe opnames zoeken…");
      await this._refreshSegs();
      if (req !== this._req || !this._cur) return;
      const seg = this._segAt(at - 1);
      if (seg) this._cur.seg = seg;
      w = this._nextWindow();
      if (w && w.end - w.start < 3) w = null;   // opname loopt nog maar is nog niet beschikbaar
    }
    if (!w && this._sel.day < todayStr(this._tz)) {
      // na middernacht verder met de volgende dag met opnames (vraag Matthias 30/10/2026)
      if (await this._nextDay()) return;
    }
    if (w) this._play(w.start, w.end, true);
    else this._setStatus(null, this._sel.day === todayStr(this._tz) ? "Einde van de opnames tot nu." : "Einde van de opnames op deze dag.");
  }

  async _nextDay() {
    const req = this._req, ch = this._sel.channel, today = todayStr(this._tz);
    const known = this._days[ch];
    let day = this._sel.day;
    for (let i = 0; i < 14; i++) {
      day = addDays(day, 1);
      if (day > today) return false;
      if (known && known.size && !known.has(day) && day !== today) continue;   // geen opnames die dag
      this._setStatus(null, `Volgende dag: ${dayLabel(day)}, opnames zoeken…`);
      this._sel.day = day; this._sel.hour = null;
      const y = +day.slice(0, 4), m = +day.slice(4, 6);
      if (!this._cal || this._cal.y !== y || this._cal.m !== m) { this._cal = { y, m }; this._loadDays(); }
      this._drawCalendar();
      await this._loadDay();
      if (req !== this._req || ch !== this._sel.channel || day !== this._sel.day) return true;   // intussen iets anders gekozen
      const first = (this._segs || [])[0];
      if (first) { this._play(first.start, Math.min(first.end, first.start + 300), true); return true; }
    }
    return false;
  }

  // stukken opnieuw ophalen zonder de tijdlijn leeg te maken
  async _refreshSegs() {
    const ch = this._sel.channel, day = this._sel.day;
    try {
      const r = await this._hass.callWS({ type: "btechnics_vigi/record/search", channel: ch, day });
      if (ch !== this._sel.channel || day !== this._sel.day) return;
      this._segs = r.segments || [];
      this._drawDay();
    } catch (e) { /* oude lijst blijft */ }
  }

  _nextWindow(from) {
    const c = from || this._cur;
    if (!c) return null;
    if (c.end < c.seg.end - 1) return { seg: c.seg, start: c.end, end: Math.min(c.seg.end, c.end + 300) };
    const n = (this._segs || []).find((s) => s.start >= c.seg.end);
    return n ? { seg: n, start: n.start, end: Math.min(n.end, n.start + 180) } : null;
  }

  _prefetchNext() {
    // het volgende venster alvast binnenhalen (zelfde grenzen als _play, dan hergebruikt de server het).
    // Vanaf 4x ook het venster daarna: de server haalt alles in volgorde binnen met max 3 replays (clips.py).
    let w = this._nextWindow();
    for (let i = 0; w && i < (this._speed >= 4 ? 2 : 1); i++, w = this._nextWindow(w)) {
      if (w.end > Date.now() / 1000 - 5) break;   // nog niet opgenomen: niet vooraf ophalen (review)
      const parts = (w.seg.parts || [[w.seg.start, w.seg.end]]).filter((p) => p[1] > w.start && p[0] < w.end);
      this._hass.callWS({ type: "btechnics_vigi/clip/start", channel: this._sel.channel, start: w.start, end: w.end, parts, prefetch: true }).catch(() => {});
    }
  }

  async _download() {
    const c = this._cur && this._cur.clip;
    if (!c || c.state !== "ready") return;
    try {
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: `/api/btechnics_vigi/clip/${c.clip_id}?download=1`, expires: 600 });
      const a = document.createElement("a");
      a.href = path; a.download = ""; a.rel = "noopener";
      this.shadowRoot.appendChild(a); a.click(); a.remove();
      this._setStatus(null, `Download gestart (${(c.size / 1048576).toFixed(1)} MB). Elke download komt in het logboek van Home Assistant.`);
    } catch (e) { this._setStatus(null, "Download mislukt: " + (e.message || e.code)); }
  }

  _stopStream() {
    if (this._cur && this._cur.streamUrl) {
      this._hass.callWS({ type: "btechnics_vigi/replay/stop", url: this._cur.streamUrl }).catch(() => {});
      this._cur.streamUrl = null;
    }
  }

  _stopAll(hide) {
    this._req = (this._req || 0) + 1;
    this._stopStream();
    this._closeLive();
    const sr = this.shadowRoot;
    const vb = sr && sr.getElementById("vbox");
    if (vb) dropMedia(vb);
    if (hide !== false) {
      // speler dicht of andere camera/dag: binnenhalen stoppen, zodat de recorder vrij is (review 30/10/2026)
      if (this._cur && this._hass) this._hass.callWS({ type: "btechnics_vigi/clip/cancel" }).catch(() => {});
      this._cur = null;
      const p = sr && sr.getElementById("player");
      if (p && hide) p.hidden = true;
      this._tick();
    }
  }
}

const CSS = `
:host { display:block; outline:none; }
ha-card { padding: 12px 16px 16px; }
button { font:inherit; color:var(--primary-text-color); }
.top { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
.tabs { display:flex; gap:6px; }
.tab { border:1px solid var(--divider-color); background:none; padding:8px 16px; border-radius:18px; cursor:pointer; }
.tab.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
.ver, .sub, .hint { color:var(--secondary-text-color); font-size:.85rem; }
.err { color:var(--error-color); margin:8px 0; }
.empty { padding:24px; color:var(--secondary-text-color); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:12px; }
.tile { border:1px solid var(--divider-color); border-radius:10px; overflow:hidden; cursor:pointer; background:var(--secondary-background-color); padding:0; text-align:left; }
.tile .img { position:relative; aspect-ratio:16/9; background:#111; }
.tile img { width:100%; height:100%; object-fit:cover; display:block; }
.tile .off { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#ddd; font-size:.9rem; pointer-events:none; }
.tile.unavail img { opacity:.25; }
.cap { display:flex; align-items:center; gap:8px; padding:8px 10px; }
.cap .ch { margin-left:auto; color:var(--secondary-text-color); font-size:.8rem; }
.dot { width:8px; height:8px; border-radius:50%; background:var(--success-color, #2e7d32); flex:none; }
.tile.unavail .dot { background:var(--error-color, #c62828); }
.hint { margin-top:10px; }
.modal { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:10; display:flex; align-items:center; justify-content:center; padding:16px; }
.modal[hidden] { display:none; }
.mbox { background:var(--card-background-color); border-radius:12px; width:min(1100px, 100%); max-height:100%; overflow:auto; }
.mhead { display:flex; justify-content:space-between; align-items:center; padding:8px 14px; }
.x { background:none; border:none; font-size:20px; cursor:pointer; width:40px; height:40px; }
.vbox { position:relative; background:#000; aspect-ratio:16/9; border-radius:8px; overflow:hidden; }
.vbox > video, .vbox > ha-hls-player { width:100%; height:100%; display:block; object-fit:contain; }
.status { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#eee; text-align:center; padding:16px; pointer-events:none; }
.status[hidden] { display:none; }
.status.err { color:#ffb4a9; }
.prog { position:absolute; left:0; right:0; bottom:0; height:4px; background:rgba(255,255,255,.15); }
.prog[hidden] { display:none; }
.prog i { display:block; height:100%; width:0; background:#ED6928; transition:width .8s; }
.rec { display:grid; grid-template-columns:280px minmax(0,1fr); gap:16px; }
.side { display:flex; flex-direction:column; gap:12px; min-width:0; }
.main { min-width:0; display:flex; flex-direction:column; gap:8px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { border:1px solid var(--divider-color); background:none; padding:6px 12px; border-radius:16px; cursor:pointer; min-height:36px; }
.chip.on { border-color:var(--primary-color); color:var(--primary-color); font-weight:600; }
.nav, .link, .ctrl button, .pc { border:1px solid var(--divider-color); background:none; border-radius:8px; min-height:36px; min-width:36px; padding:0 10px; cursor:pointer; }
.nav[disabled], .ctrl button[disabled] { opacity:.35; cursor:default; }
.link { border:none; color:var(--primary-color); }
.cal { border:1px solid var(--divider-color); border-radius:10px; padding:8px; }
.calhead { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
.calhead b { text-transform:capitalize; }
.calgrid { display:grid; grid-template-columns:repeat(7, 1fr); gap:2px; text-align:center; }
.dn { font-size:.75rem; color:var(--secondary-text-color); padding:4px 0; }
.cd { position:relative; border:none; background:none; border-radius:8px; height:36px; cursor:pointer; color:var(--secondary-text-color); }
.cd.has { color:var(--primary-text-color); font-weight:600; }
.cd i { position:absolute; left:50%; bottom:4px; width:5px; height:5px; margin-left:-2.5px; border-radius:50%; background:#ED6928; }
.cd.today { box-shadow:inset 0 0 0 1px var(--primary-color); }
.cd.sel { background:var(--primary-color); color:var(--text-primary-color, #fff); }
.cd.sel i { background:#fff; }
.cd[disabled] { opacity:.3; cursor:default; }
.calfoot { display:flex; justify-content:space-between; align-items:center; margin-top:6px; font-size:.8rem; color:var(--secondary-text-color); }
.lg { display:inline-block; width:6px; height:6px; border-radius:50%; background:#ED6928; vertical-align:middle; }
.dayhead { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.hours { display:grid; grid-template-columns:repeat(24, minmax(0,1fr)); gap:2px; }
.hr { position:relative; border:1px solid var(--divider-color); background:var(--secondary-background-color); border-radius:4px; height:40px; padding:0; cursor:pointer; overflow:hidden; }
.hr span { position:absolute; left:0; right:0; bottom:0; height:calc(var(--f) * 100%); background:var(--primary-color); opacity:.55; }
.hr em { position:relative; font-style:normal; font-size:.7rem; }
.hr.sel { border-color:#ED6928; box-shadow:0 0 0 1px #ED6928; }
.tlhead { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; margin-top:6px; }
.tlnav { display:flex; gap:6px; }
.tlwrap { position:relative; }
.tlbar { position:relative; height:44px; background:var(--secondary-background-color); border-radius:6px; cursor:crosshair; border:1px solid var(--divider-color); }
.tlbar i { position:absolute; top:6px; bottom:6px; background:var(--primary-color); opacity:.8; border-radius:2px; }
.tlbar u { position:absolute; top:0; bottom:0; width:2px; background:var(--error-color, #c62828); text-decoration:none; }
.tlbar u[hidden], .tlbar em[hidden] { display:none; }
.tlbar em { position:absolute; top:-3px; bottom:-3px; width:3px; margin-left:-1px; background:#ED6928; border-radius:2px; }
.tip { position:absolute; top:-30px; background:var(--primary-text-color); color:var(--card-background-color); font-size:.8rem; padding:3px 8px; border-radius:6px; white-space:nowrap; pointer-events:none; width:90px; text-align:center; box-sizing:border-box; }
.tip[hidden] { display:none; }
.ticks { position:relative; height:18px; font-size:.75rem; color:var(--secondary-text-color); }
.ticks span { position:absolute; transform:translateX(-50%); top:2px; white-space:nowrap; }
.ticks span:first-child { transform:none; } .ticks span:last-child { transform:translateX(-100%); }
.pieces { display:flex; flex-wrap:wrap; gap:6px; }
.player { border:1px solid var(--divider-color); border-radius:10px; padding:10px; margin-top:6px; }
.player[hidden] { display:none; }
.phead { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-bottom:8px; }
.phead b { font-size:1.4rem; font-variant-numeric:tabular-nums; }
.ctrl { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-top:8px; }
.ctrl .sep { width:1px; height:24px; background:var(--divider-color); }
.ctrl .grow { flex:1; }
.speeds { display:flex; gap:4px; }
.sp.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
.dl { border-color:#ED6928 !important; color:#ED6928 !important; }
.auto { display:flex; gap:4px; align-items:center; font-size:.85rem; }
@media (max-width: 760px) {
  ha-card { padding:10px; }
  .grid { grid-template-columns:1fr; }
  .rec { grid-template-columns:1fr; }
  .chips { flex-wrap:nowrap; overflow-x:auto; }
  .chip { white-space:nowrap; }
  .hours { grid-template-columns:repeat(12, minmax(0,1fr)); }
  .ctrl .sep, .ctrl .grow { display:none; }
  .phead b { font-size:1.15rem; }
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
    try { (el._stopAll || el._stopPlayer || (() => {})).call(el); el._init(); } catch (e) { /* volgende kaart */ }
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
// stoppen zodra er kaarten zijn en ze allemaal deze versie hebben (audit: niet 60 keer de hele pagina doorlopen)
const vigiTick = setInterval(() => {
  vigiDefine();
  const cards = vigiCards();
  if (++vigiTries >= 60 || (cards.length && cards.every((c) => c.constructor.__vigiVer === VIGI_VERSION))) clearInterval(vigiTick);
}, 2000);
window.addEventListener("load", vigiDefine);
window.addEventListener("location-changed", () => setTimeout(vigiDefine, 500));
window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "btechnics-vigi-card"))
  window.customCards.push({ type: "btechnics-vigi-card", name: "Btechnics VIGI", description: "Live raster en opnames van de VIGI recorder" });
