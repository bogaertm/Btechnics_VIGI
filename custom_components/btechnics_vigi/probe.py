"""Diagnose (enkel beheerders, enkel lezen): hoe snel levert de recorder een opname?

1. ffmpeg kopieert de replay zonder om te zetten (-c copy) en meldt de snelheid ("speed=").
2. Eigen minimale RTSP-client: PLAY met Scale- of Speed-kop, gemeten aan de RTP-tijdstempels
   (mediaseconden per wandklokseconde). Zo weten we of versneld binnenhalen kan.
Wachtwoorden komen nooit in het resultaat (URL's worden afgeschermd).
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
from urllib.parse import urlsplit

from .api import digest_header, parse_challenge


def redact(s: str) -> str:
    return re.sub(r"rtsp://[^@/\s]*@", "rtsp://***@", s)


async def ffmpeg_speed(binary: str, url: str, seconds: float = 15.0) -> dict:
    proc = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-nostdin", "-rtsp_transport", "tcp", "-i", url, "-map", "0:v:0",
        "-c", "copy", "-f", "null", "-", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    buf = b""
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            try:
                chunk = await asyncio.wait_for(proc.stderr.read(4096), timeout=1)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    txt = buf.decode(errors="replace")
    speeds = re.findall(r"speed=\s*([\d.]+)x", txt)
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", txt)
    media = (int(times[-1][0]) * 3600 + int(times[-1][1]) * 60 + float(times[-1][2])) if times else 0
    codec = re.search(r"Video: (\w+)[^\n]*?(\d{3,5}x\d{3,5})", txt)
    return {"wand_s": round(time.monotonic() - t0, 1), "media_s": round(media, 1),
            "speed": float(speeds[-1]) if speeds else None,
            "video": f"{codec.group(1)} {codec.group(2)}" if codec else None,
            "einde": "stream ended" if proc.returncode == 0 else None,
            "fout": redact(txt[-300:]) if not times else None}


class _Rtsp:
    def __init__(self, url: str, username: str, password: str) -> None:
        u = urlsplit(url)
        self.host, self.port = u.hostname, u.port or 554
        self.url = f"rtsp://{self.host}:{self.port}{u.path}" + (f"?{u.query}" if u.query else "")
        self.user, self.pw = username, password
        self.cseq = 0
        self.session = None
        self.auth = None

    async def open(self):
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)

    async def _read_response(self):
        # interleaved RTP kan tussen antwoorden zitten: overslaan
        while True:
            first = await self.r.readexactly(1)
            if first == b"$":
                hdr = await self.r.readexactly(3)
                await self.r.readexactly(struct.unpack(">H", hdr[1:])[0])
                continue
            line = first + await self.r.readline()
            if line.startswith(b"RTSP/"):
                break
        status = int(line.split()[1])
        headers = {}
        while True:
            h = (await self.r.readline()).decode(errors="replace").strip()
            if not h:
                break
            k, _, v = h.partition(":")
            headers.setdefault(k.strip().lower(), []).append(v.strip())
        body = b""
        if "content-length" in headers:
            body = await self.r.readexactly(int(headers["content-length"][0]))
        return status, headers, body

    async def req(self, method, uri=None, extra=None):
        uri = uri or self.url
        for poging in (1, 2):
            self.cseq += 1
            lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: btechnics-vigi"]
            if self.session:
                lines.append(f"Session: {self.session}")
            if self.auth:
                lines.append("Authorization: " + digest_header(self.auth, self.user, self.pw, method, uri))
            for k, v in (extra or {}).items():
                lines.append(f"{k}: {v}")
            self.w.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await self.w.drain()
            status, headers, body = await asyncio.wait_for(self._read_response(), 10)
            if status == 401 and poging == 1:
                chs = headers.get("www-authenticate", [])
                pick = next((c for c in chs if "SHA-256" in c.upper()), None) or (chs[0] if chs else "")
                self.auth = parse_challenge(pick)
                continue
            return status, headers, body
        return status, headers, body

    def close(self):
        try:
            self.w.close()
        except Exception:  # noqa: BLE001
            pass


async def rtsp_rate(url: str, username: str, password: str, extra: dict | None, seconds: float = 8.0) -> dict:
    c = _Rtsp(url, username, password)
    out: dict = {"koppen": extra or {}}
    try:
        await c.open()
        st, h, body = await c.req("DESCRIBE", extra={"Accept": "application/sdp"})
        out["describe"] = st
        if st != 200:
            return out
        sdp = body.decode(errors="replace")
        base = (h.get("content-base") or [c.url])[0]
        # eerste videotrack
        video = sdp.split("m=video", 1)[1] if "m=video" in sdp else sdp
        ctrl = re.search(r"a=control:(\S+)", video)
        rate = re.search(r"a=rtpmap:\d+ (\w+)/(\d+)", video)
        out["codec"] = rate.group(1) if rate else None
        clock = int(rate.group(2)) if rate else 90000
        cu = ctrl.group(1) if ctrl else ""
        track = cu if cu.startswith("rtsp://") else base.rstrip("/") + "/" + cu.lstrip("/")
        st, h, _ = await c.req("SETUP", uri=track, extra={"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        out["setup"] = st
        if st != 200:
            return out
        c.session = h["session"][0].split(";")[0]
        play_extra = {"Range": "npt=0.000-"}
        play_extra.update(extra or {})
        st, h, _ = await c.req("PLAY", uri=base, extra=play_extra)
        out["play"] = st
        out["play_koppen"] = {k: v[0] for k, v in h.items() if k in ("scale", "speed", "range", "rtp-info")}
        if st != 200:
            return out
        first_ts = last_ts = None
        wraps = 0
        nbytes = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                b = await asyncio.wait_for(c.r.readexactly(1), 3)
            except asyncio.TimeoutError:
                break
            if b != b"$":
                await c.r.readline()   # RTSP-bericht van de server, regel overslaan
                continue
            hdr = await c.r.readexactly(3)
            n = struct.unpack(">H", hdr[1:])[0]
            pkt = await c.r.readexactly(n)
            nbytes += n
            if hdr[0] != 0 or n < 12:
                continue
            ts = struct.unpack(">I", pkt[4:8])[0]
            if first_ts is None:
                first_ts = ts
            elif last_ts is not None and ts < last_ts and last_ts - ts > 2**31:
                wraps += 1
            last_ts = ts
        wall = time.monotonic() - t0
        if first_ts is not None:
            media = ((last_ts + wraps * 2**32) - first_ts) / clock
            out.update({"wand_s": round(wall, 1), "media_s": round(media, 1),
                        "snelheid": round(media / wall, 2) if wall else None, "kB_s": round(nbytes / 1024 / wall)})
        try:
            await c.req("TEARDOWN", uri=base)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001  diagnose
        out["fout"] = redact(f"{e.__class__.__name__}: {e}")[:200]
    finally:
        c.close()
    return out
