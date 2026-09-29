"""Clip import against a local web server."""
import asyncio
import functools
import http.server
import subprocess
import threading
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import importer, main
from app.db import DB


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/redirect-internal"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/boo.mp3")
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2",
                    "-c:a", "libmp3lame", str(root / "boo.mp3")], check=True)
    (root / "page.html").write_text('<html><body><audio src="boo.mp3" controls></audio></body></html>')
    (root / "empty.html").write_text("<html><body>nothing here</body></html>")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_STREAMS", True)
    (tmp_path / "clips").mkdir()
    main.db = DB(tmp_path / "t.db")
    main.hub = main.Hub(None, main.db, "quad")
    return TestClient(main.app, headers={"X-authentik-username": "tester"})


def clips(client):
    return [s["name"] for s in client.get("/api/sources").json() if s["kind"] == "clip"]


def leftovers(tmp_path):
    return list(tmp_path.glob(".import-*")) + list((tmp_path / "clips").glob(".import-*"))


def test_direct_audio_link(client, site, tmp_path):
    r = client.post("/api/clips/import", json={"name": "boo", "url": f"{site}/boo.mp3"})
    assert r.status_code == 200, r.text
    assert (tmp_path / "clips" / f"{r.json()['id']}.m4a").stat().st_size > 0
    assert clips(client) == ["boo"] and not leftovers(tmp_path)


def test_page_with_embedded_audio_resolved_by_ytdlp(client, site, tmp_path):
    r = client.post("/api/clips/import", json={"name": "page", "url": f"{site}/page.html"})
    assert r.status_code == 200, r.text
    assert not leftovers(tmp_path)


def test_page_without_audio_fails_cleanly(client, site, tmp_path):
    r = client.post("/api/clips/import", json={"name": "nope", "url": f"{site}/empty.html"})
    assert r.status_code == 400 and "couldn't find any audio" in r.text
    assert clips(client) == [] and list((tmp_path / "clips").iterdir()) == [] and not leftovers(tmp_path)


def test_private_address_refused_by_default(client, site, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_PRIVATE_STREAMS", False)
    r = client.post("/api/clips/import", json={"name": "x", "url": f"{site}/boo.mp3"})
    assert r.status_code == 400 and "private" in r.text


def test_redirect_to_internal_address_refused(client, site, monkeypatch):
    # Pretend 127.0.0.1 is public and "localhost" is internal: the second hop must be refused.
    monkeypatch.setattr(main, "ALLOW_PRIVATE_STREAMS", False)
    monkeypatch.setattr(importer, "public_host", lambda url: urlsplit(url).hostname != "localhost")
    r = client.post("/api/clips/import", json={"name": "x", "url": f"{site}/redirect-internal"})
    assert r.status_code == 400 and "private" in r.text


def test_size_cap(client, site, tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_BYTES", 1000)
    r = client.post("/api/clips/import", json={"name": "big", "url": f"{site}/boo.mp3"})
    assert r.status_code == 400 and "larger than" in r.text
    assert not leftovers(tmp_path)


def test_too_long_is_trimmed(client, site, tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_SECONDS", 1)
    r = client.post("/api/clips/import", json={"name": "short", "url": f"{site}/boo.mp3"})
    assert r.status_code == 200
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(tmp_path / "clips" / f"{r.json()['id']}.m4a")], capture_output=True, text=True)
    assert float(out.stdout) <= 1.1


def test_duplicate_name_is_409_before_downloading(client, site):
    assert client.post("/api/clips/import", json={"name": "boo", "url": f"{site}/boo.mp3"}).status_code == 200
    r = client.post("/api/clips/import", json={"name": "boo", "url": f"{site}/boo.mp3"})
    assert r.status_code == 409


def test_second_import_while_busy_is_429(client, site):
    async def hold():
        async with importer._one_at_a_time:
            return client.post("/api/clips/import", json={"name": "b", "url": f"{site}/boo.mp3"})
    # TestClient runs the app in its own loop; holding the semaphore from here marks it busy.
    r = asyncio.run(hold())
    assert r.status_code == 429


def test_live_or_segmented_formats_are_refused(tmp_path, monkeypatch):
    async def fake_resolve_proc(*a, **kw):
        class P:
            returncode = 0

            async def communicate(self):
                return b'{"url": "https://example.com/x.m3u8", "protocol": "m3u8_native"}', b""
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_resolve_proc)
    with pytest.raises(importer.ImportFailed, match="segmented"):
        asyncio.run(importer._resolve("https://example.com/", tmp_path))


def test_sweep_removes_stale_workdirs(tmp_path):
    (tmp_path / ".import-abc" / "sub").mkdir(parents=True)
    importer.sweep(tmp_path)
    assert not list(tmp_path.glob(".import-*"))


def test_radio_stream_is_refused_quickly(client, monkeypatch):
    import httpx

    def radio(request):
        return httpx.Response(200, headers={"content-type": "audio/mpeg", "icy-name": "Radio"}, content=b"\xff" * 10)
    real = httpx.AsyncClient
    monkeypatch.setattr(importer.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(radio), **kw))
    r = client.post("/api/clips/import", json={"name": "radio", "url": "http://127.0.0.1:1/stream"})
    assert r.status_code == 400 and "Stations" in r.text
