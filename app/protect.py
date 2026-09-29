"""Async client for the (private) UniFi Protect API: login, speakers, talkback, native TTS."""
import asyncio
import logging
import ssl
import uuid

import httpx
import websockets

log = logging.getLogger(__name__)


class TalkbackRefused(Exception):
    """Protect closed the talkback socket; code 4403 means the account lacks speaker permission."""

    def __init__(self, code):
        self.code = code
        msg = ("this account lacks Protect permission to use the speaker" if code == 4403
               else f"talkback socket closed by Protect (code {code})")
        super().__init__(msg)


def _insecure_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Protect:
    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password
        self.api = f"https://{host}/proxy/protect/api"
        self._client = httpx.AsyncClient(verify=False, timeout=20)
        self._lock = asyncio.Lock()

    async def close(self):
        await self._client.aclose()

    async def login(self):
        async with self._lock:
            self._client.cookies.clear()
            self._client.headers.pop("X-CSRF-Token", None)
            r = await self._client.post(f"https://{self.host}/api/auth/login",
                                        json={"username": self.username, "password": self.password})
            r.raise_for_status()
            csrf = r.headers.get("X-Updated-Csrf-Token") or r.headers.get("X-CSRF-Token")
            if csrf:
                self._client.headers["X-CSRF-Token"] = csrf

    @property
    def token(self):
        return self._client.cookies.get("TOKEN")

    async def _request(self, method, url, **kw):
        if not self.token:
            await self.login()
        r = await self._client.request(method, url, **kw)
        if r.status_code == 401:
            await self.login()
            r = await self._client.request(method, url, **kw)
        return r

    async def speakers(self):
        r = await self._request("GET", f"{self.api}/bootstrap")
        if not r.is_success or "json" not in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"Protect bootstrap failed: HTTP {r.status_code} "
                               f"(is Protect running on {self.host}?)")
        return [{"id": s["id"], "mac": s.get("mac"), "name": s.get("name") or s.get("mac"),
                 "state": s.get("state"), "volume": s.get("volume")}
                for s in r.json().get("speakers", [])]

    async def speak(self, speaker_mac, text):
        """Protect's built-in TTS (the console's "Test Alarm" dry-run). Plays on the speaker itself."""
        body = {
            "name": "_horn_player", "enable": True, "sources": [],
            "conditions": [{"condition": {"type": "is", "source": "webhook", "value": str(uuid.uuid4())}}],
            "historyConditions": [], "schedules": [],
            "actions": [{"type": "PLAY_TEXT_ON_SPEAKER", "order": -1, "metadata": {
                "text": text, "tone": "welcome", "type": "custom",
                "sources": [{"type": "include", "device": speaker_mac}]}}],
            "cooldown": {"enable": False, "timeout": 0},
        }
        r = await self._request("POST", f"{self.api}/automations/run", json=body)
        if r.status_code in (401, 403):
            raise PermissionError("this account lacks Protect permission to run announcements")
        r.raise_for_status()

    async def open_talkback(self, speaker_id):
        if not self.token:
            await self.login()
        for attempt in range(2):
            try:
                ws = await websockets.connect(
                    f"wss://{self.host}/proxy/protect/ws/talkback?speaker={speaker_id}",
                    additional_headers={"Cookie": f"TOKEN={self.token}"},
                    origin=f"https://{self.host}", ssl=_insecure_ctx(), open_timeout=15,
                    ping_interval=None, max_queue=4)
                break
            except websockets.InvalidStatus as e:
                # An expired session is refused at the handshake; log in again once.
                if attempt or e.response.status_code not in (401, 403):
                    raise
                await self.login()
        # Protect accepts the handshake and then closes straight away if talkback isn't allowed.
        try:
            await asyncio.wait_for(ws.recv(), 0.4)
        except asyncio.TimeoutError:
            pass
        except websockets.ConnectionClosed as e:
            raise TalkbackRefused(e.rcvd.code if e.rcvd else None) from None
        return ws
