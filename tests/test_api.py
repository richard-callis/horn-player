"""HTTP-level checks with the app's globals wired to a temp DB (no Protect needed)."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import DB


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    main.db = DB(tmp_path / "t.db")
    main.hub = main.Hub(None, main.db, "quad")
    return TestClient(main.app, headers={"X-authentik-username": "tester"})  # no lifespan


def test_requires_auth_header(client):
    assert TestClient(main.app).get("/api/sources").status_code == 401


def test_cross_site_write_refused(client):
    r = client.post("/api/playlists", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_upload_name_clash_saves_nothing(client, tmp_path):
    pid = client.post("/api/playlists", json={"name": "spooky"}).json()["id"]
    assert client.post(f"/api/playlists/{pid}/tracks", files=[("files", ("a.mp3", b"1"))]).status_code == 200
    r = client.post(f"/api/playlists/{pid}/tracks", files=[("files", ("b.mp3", b"2")), ("files", ("a.mp3", b"3"))])
    assert r.status_code == 409
    assert sorted(p.name for p in (tmp_path / "music" / str(pid)).iterdir()) == ["a.mp3"]


def test_path_traversal_names_are_flattened(client, tmp_path):
    pid = client.post("/api/playlists", json={"name": "p"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", files=[("files", ("../../evil.mp3", b"x"))])
    assert (tmp_path / "music" / str(pid) / "evil.mp3").exists()
    assert not (tmp_path / "evil.mp3").exists()
