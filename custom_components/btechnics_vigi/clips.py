"""Opnames als MP4 binnenhalen (zonder omzetten), om te spoelen, versneld af te spelen en te downloaden.

Gemeten op de recorder (30/10/2026, NVR1008H fw 1.6.0):
- een replay komt altijd op normale snelheid (RTSP Scale/Speed/Rate-Control worden genegeerd);
- maximaal 4 replays tegelijk, de 5e krijgt een 4xx;
- een replay stopt niet op endtime en springt over gaten.
Daarom: per doorlopend opgenomen deel een eigen replay met -t (exacte lengte, geen gaten), tot 3 tegelijk,
daarna samenvoegen met -c copy. De tijdsindeling (klok per deel) gaat mee naar de speler.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.core import HomeAssistant

from .api import VigiClient

_LOGGER = logging.getLogger(__name__)
MAX_CLIP_S = 15 * 60          # venster per keer (ongeveer 5 min binnenhalen met 3 delen)
PARALLEL = 3                  # max delen tegelijk
MAX_SESSIONS = 4              # recorder: max 4 replays, samen met de streams van ReplayManager (review 30/10/2026)
RECENT_S = 20 * 60            # recent gebruikte bestanden niet wissen bij de 1,5 GB-grens
MIN_FREE_BYTES = 1_000_000_000
KEEP_S = 2 * 3600
MAX_TOTAL_BYTES = 1_500_000_000
_TIME_RE = re.compile(rb"time=(\d+):(\d+):([\d.]+)")
_BUSY_RE = re.compile(r"4XX|Client Error|failed: 4\d\d|Connection refused|timed out", re.I)


def _mask(txt: str) -> str:
    """Wachtwoord uit RTSP-adressen halen, eerst op de volledige tekst (review: knippen kon het half laten staan)."""
    return re.sub(r"rtsp://[^@/\s]*@", "rtsp://***@", txt)


class _Slots:
    """Plaatsen op de recorder voor het binnenhalen, met voorrang: eerst het venster dat nu bekeken wordt, dan
    vooraf ophalen. Het aantal plaatsen houdt rekening met lopende streams (max 4 samen)."""

    def __init__(self, capacity) -> None:
        self._capacity = capacity
        self._busy = 0
        self._waiters: list[tuple[int, Clip, asyncio.Future]] = []
        self._seq = 0

    def _wake(self) -> None:
        while self._waiters and self._busy < self._capacity():
            self._waiters.sort(key=lambda w: (w[1].prio, w[0]))
            _, _, fut = self._waiters.pop(0)
            if not fut.done():
                self._busy += 1
                fut.set_result(None)

    async def acquire(self, clip: Clip) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        item = (self._seq, clip, fut)
        self._waiters.append(item)
        self._wake()
        try:
            await fut
        except asyncio.CancelledError:
            if item in self._waiters:
                self._waiters.remove(item)
            elif fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self) -> None:
        self._busy = max(0, self._busy - 1)
        self._wake()

    def poke(self) -> None:
        self._wake()


def split_pieces(parts: list[list[int]], start: int, end: int, max_piece: int = 120) -> list[tuple[int, int]]:
    """Doorlopende deelstukken binnen [start, end]; lange delen in stukken van max max_piece seconden."""
    out: list[tuple[int, int]] = []
    for a, b in sorted(parts):
        a, b = max(a, start), min(b, end)
        if b - a < 1:
            continue
        n = max(1, -(-(b - a) // max_piece))
        step = (b - a) / n
        for i in range(n):
            s = int(round(a + i * step))
            e = int(round(a + (i + 1) * step)) if i < n - 1 else b
            if e > s:
                out.append((s, e))
    return out


@dataclass
class Clip:
    id: str
    channel: int
    channel_name: str
    start: int
    end: int
    pieces: list[tuple[int, int]]
    user: str
    created: float = field(default_factory=time.time)
    prio: int = 1                # 0 = wordt bekeken, 1 = vooraf ophalen
    used: float = field(default_factory=time.time)
    viewers: set = field(default_factory=set)
    state: str = "fetching"     # fetching | joining | ready | error
    error: str | None = None
    done_s: list[float] = field(default_factory=list)
    lengths: list[float] = field(default_factory=list)
    codec: str | None = None
    path: Path | None = None
    size: int = 0
    task: asyncio.Task | None = None

    def status(self) -> dict:
        total = sum(e - s for s, e in self.pieces) or 1
        prog = min(1.0, sum(self.done_s) / total) if self.state == "fetching" else (1.0 if self.state != "error" else 0)
        clock = []
        if self.state == "ready":
            for (s, _e), ln in zip(self.pieces, self.lengths):
                clock.append({"clock": s, "len": round(ln, 2)})
        return {"clip_id": self.id, "state": self.state, "progress": round(prog, 3), "error": self.error,
                "channel": self.channel, "start": self.start, "end": self.end, "size": self.size,
                "codec": self.codec, "map": clock, "pieces": len(self.pieces)}


class ClipManager:
    def __init__(self, hass: HomeAssistant, client: VigiClient, folder: Path, streams=lambda: 0) -> None:
        self.hass = hass
        self.client = client
        self.folder = folder
        self.clips: dict[str, Clip] = {}
        self._streams = streams
        self._slots = _Slots(lambda: max(1, min(PARALLEL, MAX_SESSIONS - self._streams())))
        self._lock = asyncio.Lock()

    def _binary(self) -> str:
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415
            return get_ffmpeg_manager(self.hass).binary
        except Exception:  # noqa: BLE001
            return "ffmpeg"

    async def start(self, channel: int, channel_name: str, start: int, end: int, parts: list[list[int]],
                    user: str, prefetch: bool = False) -> Clip:
        async with self._lock:   # review: twee gelijktijdige aanvragen voor hetzelfde venster haalden het dubbel binnen
            return await self._start(channel, channel_name, start, end, parts, user, prefetch)

    async def _start(self, channel, channel_name, start, end, parts, user, prefetch) -> Clip:
        end = min(end, start + MAX_CLIP_S, int(time.time()) - 5)
        if end <= start:
            raise ValueError("geen opname in dit venster")
        if not prefetch:
            # nieuw venster om te bekijken: wat deze gebruiker nog aan het binnenhalen was, is niet meer nodig
            # (review: oude vensters hielden de plaatsen bezet, het nieuwe kwam pas na 3 tot 4 min)
            for c in list(self.clips.values()):
                if c.user == user and c.state == "fetching" and not (c.channel == channel and c.start == start):
                    self._cancel(c)
        # zelfde venster al aanwezig of bezig: hergebruiken
        for c in self.clips.values():
            if c.channel == channel and c.start == start and c.end == end and c.state != "error":
                c.used = time.time()
                if not prefetch and c.prio:
                    c.prio = 0
                    self._slots.poke()
                return c
        # zo verdelen dat er 3 delen tegelijk lopen (min 8 s per deel, max 2 min)
        rec = sum(max(0, min(b, end) - max(a, start)) for a, b in (parts or [[start, end]]))
        pieces = split_pieces(parts or [[start, end]], start, end, max_piece=min(120, max(8, -(-rec // PARALLEL))))
        if not pieces:
            raise ValueError("geen opname in dit venster")
        await self._cleanup_async()
        free = (await self.hass.async_add_executor_job(shutil.disk_usage, self._ensure_folder())).free
        if free < MIN_FREE_BYTES:
            raise ValueError("te weinig vrije schijfruimte op Home Assistant")
        clip = Clip(secrets.token_urlsafe(12), channel, channel_name, start, end, pieces, user,
                    done_s=[0.0] * len(pieces), lengths=[0.0] * len(pieces), prio=1 if prefetch else 0)
        self.clips[clip.id] = clip
        clip.task = self.hass.async_create_background_task(self._run(clip), f"btechnics_vigi clip {clip.id}")
        return clip

    def _ensure_folder(self) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        return str(self.folder)

    def _cancel(self, clip: Clip) -> None:
        if clip.task and not clip.task.done():
            clip.task.cancel()

    async def cancel_user(self, user: str, keep: set[str] | None = None) -> int:
        n = 0
        for c in list(self.clips.values()):
            if c.user == user and c.state in ("fetching", "joining") and c.id not in (keep or set()):
                self._cancel(c)
                n += 1
        return n

    async def _run(self, clip: Clip) -> None:
        work = self.folder / f"{clip.id}.tmp"
        tasks: list[asyncio.Task] = []
        try:
            await self.hass.async_add_executor_job(lambda: work.mkdir(parents=True, exist_ok=True))
            # review: bij een fout in een deel liepen de andere delen door (ffmpeg en recorderplaatsen bleven bezet)
            tasks = [asyncio.ensure_future(self._piece(clip, i, work)) for i in range(len(clip.pieces))]
            try:
                await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            clip.state = "joining"
            out = self.folder / f"{clip.id}.mp4"
            await self._join(clip, work, out)
            clip.path = out
            clip.size = (await self.hass.async_add_executor_job(out.stat)).st_size
            clip.state = "ready"
        except asyncio.CancelledError:
            clip.state, clip.error = "error", "gestopt"
            raise
        except Exception as e:  # noqa: BLE001  fout naar de kaart
            clip.state, clip.error = "error", str(e)[:200]
            _LOGGER.warning("Opname binnenhalen mislukt (kanaal %s): %s", clip.channel, e)
        finally:
            await self.hass.async_add_executor_job(shutil.rmtree, str(work), True)

    async def _piece(self, clip: Clip, i: int, work: Path) -> None:
        s, e = clip.pieces[i]
        url = self.client.replay_url(clip.channel, s, e)
        target = work / f"p{i:04d}.mp4"
        for poging in range(10):
            await self._slots.acquire(clip)
            try:
                ok, err = await self._ffmpeg_piece(clip, i, url, e - s, target)
            finally:
                self._slots.release()
            if ok:
                return
            # 4xx = recorder heeft al 4 replays open (eigen app, andere kijker): even wachten
            if _BUSY_RE.search(err):
                await asyncio.sleep(3 + poging)
                continue
            raise RuntimeError(f"deel {i + 1}: {err}")
        raise RuntimeError("recorder bezet (max 4 afspeelsessies), later opnieuw proberen")

    async def _ffmpeg_piece(self, clip: Clip, i: int, url: str, dur: int, target: Path) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            self._binary(), "-hide_banner", "-nostdin", "-y", "-rtsp_transport", "tcp", "-timeout", "15000000",
            "-i", url, "-map", "0:v:0", "-c", "copy", "-t", str(dur), "-f", "mp4", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        tail = b""
        last = 0.0
        try:
            while True:
                chunk = await asyncio.wait_for(proc.stderr.read(4096), timeout=60)
                if not chunk:
                    break
                tail = (tail + chunk)[-4000:]
                if clip.codec is None:
                    m = re.search(rb"Video: (\w+)", tail)
                    if m:
                        clip.codec = m.group(1).decode()
                for m in _TIME_RE.finditer(chunk):
                    last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    clip.done_s[i] = min(last, dur)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "timed out"
        except asyncio.CancelledError:
            proc.kill()
            raise
        txt = tail.decode(errors="replace")
        if proc.returncode != 0 or last <= 0:
            return False, _mask(txt)[-300:]
        clip.done_s[i] = dur
        clip.lengths[i] = last
        return True, ""

    async def _join(self, clip: Clip, work: Path, out: Path) -> None:
        lst = work / "lijst.txt"
        files = [work / f"p{i:04d}.mp4" for i in range(len(clip.pieces))]
        await self.hass.async_add_executor_job(
            lst.write_text, "".join(f"file '{f.name}'\n" for f in files))
        args = [self._binary(), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c", "copy", "-movflags", "+faststart"]
        if (clip.codec or "").lower() in ("hevc", "h265"):
            args += ["-tag:v", "hvc1"]   # nodig voor H.265 in mp4 in Chrome en Safari
        args.append(str(out))
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError("samenvoegen mislukt: " + err.decode(errors="replace")[-200:])

    def _protected(self) -> set[str]:
        """Namen die niet weg mogen: lopende vensters (.tmp) en recent bekeken of gevraagde bestanden (review)."""
        now = time.time()
        keep = set()
        for c in self.clips.values():
            if c.state in ("fetching", "joining") or now - c.used < RECENT_S:
                keep.update({f"{c.id}.mp4", f"{c.id}.tmp"})
        return keep

    async def _cleanup_async(self) -> None:
        await self.hass.async_add_executor_job(self._cleanup, self._protected())
        # de lijst van clips enkel in de event loop aanpassen (review: dict wijzigde in een thread)
        now = time.time()
        for cid, c in list(self.clips.items()):
            gone = c.state == "ready" and c.path and not await self.hass.async_add_executor_job(c.path.exists)
            if (now - c.created > KEEP_S and now - c.used > RECENT_S and c.state not in ("fetching", "joining")) or gone:
                self.clips.pop(cid, None)

    def _cleanup(self, keep: set[str] | None = None) -> None:
        """Bestanden ouder dan 2 uur weg; totaal max 1,5 GB (oudste eerst); lopende en recent gebruikte blijven."""
        if not self.folder.exists():
            return
        keep = keep or set()
        now = time.time()
        files = []
        total = 0
        for f in self.folder.iterdir():
            try:
                st = f.stat()
                size = st.st_size if f.is_file() else sum(x.stat().st_size for x in f.iterdir())
            except OSError:
                continue
            total += size
            if f.name in keep:
                continue
            if now - st.st_mtime > KEEP_S:
                shutil.rmtree(f, True) if f.is_dir() else f.unlink(missing_ok=True)
                total -= size
                continue
            if f.is_file():
                files.append((st.st_mtime, size, f))
        files.sort()
        while files and total > MAX_TOTAL_BYTES:
            _, sz, f = files.pop(0)
            f.unlink(missing_ok=True)
            total -= sz

    async def stop_all(self) -> None:
        tasks = [c.task for c in self.clips.values() if c.task and not c.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.hass.async_add_executor_job(self._cleanup, set())


def clip_filename(clip: Clip, tz) -> str:
    from datetime import datetime  # noqa: PLC0415
    t = datetime.fromtimestamp(clip.start, tz)
    name = re.sub(r"[^A-Za-z0-9]+", "_", clip.channel_name).strip("_") or f"kanaal{clip.channel}"
    return f"{name}_{t:%Y-%m-%d_%Hu%Mm%S}.mp4"


def file_exists(p: Path | None) -> bool:
    return bool(p) and os.path.exists(p)
