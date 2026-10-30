"""Nagebootste VIGI NVR volgens OpenAPI v1.4: Digest SHA-256 op /openapi/token, Bearer, percent-encoded JSON."""
import hashlib
from urllib.parse import quote

from aiohttp import web

USER, PASS, REALM, NONCE = "homeassistant", "Scouting-test@&", "TP-LINK NVR", "5b032b462e0ad5ea"
DAY = "20261030"


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class FakeNVR:
    def __init__(self):
        self.tokens = set()
        self.calls = []
        self.segments = [(1793350000 + i * 30, 1793350000 + i * 30 + 30) for i in range(150)] + [(1793360000, 1793360100)]
        self.expire_next = False

    def app(self):
        a = web.Application()
        a.router.add_get("/openapi/token", self.token)
        for p in ("module_list", "added_devices", "snapshot", "record/days", "record/search/free_process",
                  "record/search/results"):
            a.router.add_get("/openapi/" + p, self.api)
        return a

    async def token(self, req):
        q = req.query
        if q.get("grant_type") == "refresh_token":
            if q.get("refresh_token") != "ref/ok+":
                return web.Response(status=401)
            return self._tok()
        auth = req.headers.get("Authorization", "")
        if not auth.startswith("Digest"):
            return web.Response(status=401, headers={"WWW-Authenticate":
                f'Digest realm="{REALM}", nonce="{NONCE}", algorithm="SHA-256", url="/openapi/token"'})
        f = dict(x.strip().split("=", 1) for x in auth[7:].split(","))
        f = {k: v.strip('"') for k, v in f.items()}
        ha1, ha2 = sha(f"{f['username']}:{REALM}:{PASS}"), sha("GET:/openapi/token")
        if f.get("username") != USER or f.get("response") != sha(f"{ha1}:{NONCE}:{ha2}"):
            return web.Response(status=401)
        return self._tok()

    def _tok(self):
        t = f"tok/{len(self.tokens)}+="
        self.tokens.add(t)
        return web.json_response({"token_type": "bearer", "expires_in": 1800,
                                  "access_token": quote(t, safe=""), "refresh_token": quote("ref/ok+", safe="")})

    async def api(self, req):
        tok = req.headers.get("Authorization", "")[7:]
        if self.expire_next:
            self.expire_next = False
            self.tokens.clear()
        if tok not in self.tokens:
            return web.Response(status=401)
        p = req.path[len("/openapi/"):]
        self.calls.append(p)
        if p == "module_list":
            return web.json_response({"module_list": [{"name": "recording", "version": 1}], "error_code": 0})
        if p == "added_devices":
            return web.json_response({"devices": [
                {"id": 1, "name": quote("Opslag"), "alias": "IPC", "online": "1", "ip": "192.168.6.81", "mac": "x"},
                {"id": 3, "name": quote("Cafe toog"), "alias": "Dahua", "online": "0", "ip": "192.168.6.90", "mac": "y"}],
                "error_code": 0})
        if p == "snapshot":
            return web.Response(body=b"\xff\xd8jpeg", content_type="image/jpeg")
        if p == "record/days":
            return web.json_response({"days": [{"day": "20261029"}, {"day": DAY}], "error_code": 0})
        if p == "record/search/free_process":
            return web.json_response({"process": 6, "error_code": 0})
        if p == "record/search/results":
            s, e = int(req.query["start_index"]), int(req.query["end_index"])
            if e - s + 1 > 100:
                return web.json_response({"error_code": -40201})
            if s >= len(self.segments):
                return web.json_response({"error_code": -40205})
            res = [{"start_time": str(a), "end_time": str(b)} for a, b in self.segments[s:e + 1]]
            return web.json_response({"results": res, "error_code": 0})
        return web.Response(status=404)
