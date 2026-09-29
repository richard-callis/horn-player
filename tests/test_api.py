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


def _caf_bytes(tmp_path):
    import subprocess
    src = tmp_path / "memo.caf"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "pcm_s16le", str(src)], check=True)
    return src.read_bytes()


def test_caf_clip_is_converted_to_m4a(client, tmp_path):
    (tmp_path / "clips").mkdir()
    r = client.post("/api/clips", data={"name": "memo"}, files={"file": ("New Recording.caf", _caf_bytes(tmp_path))})
    assert r.status_code == 200, r.text
    stored = list((tmp_path / "clips").iterdir())
    assert [p.suffix for p in stored] == [".m4a"]
    assert client.get(f"/api/clips/{r.json()['id']}/audio").status_code == 200


def test_caf_track_lands_as_m4a_and_garbage_is_rejected(client, tmp_path):
    pid = client.post("/api/playlists", json={"name": "memos"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/tracks", files=[("files", ("memo.caf", _caf_bytes(tmp_path)))])
    assert r.status_code == 200, r.text
    r = client.post(f"/api/playlists/{pid}/tracks", files=[("files", ("junk.caf", b"not audio at all"))])
    assert r.status_code == 400 and "couldn't read it as audio" in r.text
    assert sorted(p.name for p in (tmp_path / "music" / str(pid)).iterdir()) == ["memo.m4a"]
