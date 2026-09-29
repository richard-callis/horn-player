"""Clip import against a local web server (private addresses allowed only in these tests)."""
import functools
import http.server
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from app import importer, main
from app.db import DB


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2",
                    "-c:a", "libmp3lame", str(root / "boo.mp3")], check=True)
    (root / "page.html").write_text('<html><body><audio src="boo.mp3" controls></audio></body></html>')
    (root / "empty.html").write_text("<html><body>nothing here</body></html>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


def test_direct_audio_link(client, site, tmp_path):
    r = client.post("/api/clips/import", json={"name": "boo", "url": f"{site}/boo.mp3"})
    assert r.status_code == 200, r.text
    assert (tmp_path / "clips" / f"{r.json()['id']}.m4a").stat().st_size > 0
    assert clips(client) == ["boo"]
    assert not list(tmp_path.glob(".import-*"))           # work dir cleaned up


def test_page_with_embedded_audio_via_ytdlp(client, site):
    r = client.post("/api/clips/import", json={"name": "page", "url": f"{site}/page.html"})
    assert r.status_code == 200, r.text


def test_page_without_audio_fails_cleanly(client, site, tmp_path):
    r = client.post("/api/clips/import", json={"name": "nope", "url": f"{site}/empty.html"})
    assert r.status_code == 400
    assert clips(client) == [] and list((tmp_path / "clips").iterdir()) == []


def test_private_address_refused_by_default(client, site, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_PRIVATE_STREAMS", False)
    r = client.post("/api/clips/import", json={"name": "x", "url": f"{site}/boo.mp3"})
    assert r.status_code == 400 and "private" in r.text


def test_too_long_is_trimmed(client, site, tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_SECONDS", 1)
    r = client.post("/api/clips/import", json={"name": "short", "url": f"{site}/boo.mp3"})
    assert r.status_code == 200
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(tmp_path / "clips" / f"{r.json()['id']}.m4a")], capture_output=True, text=True)
    assert float(out.stdout) <= 1.1
