"""horn-player: play stations, playlists, sound effects and announcements on UniFi Protect speakers."""
import asyncio
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .db import DB
from .player import AUDIO_EXT, Interrupt, Music, SpeakerPlayer
from .protect import Protect
from .scheduler import Scheduler
from .tts import synthesize

log = logging.getLogger("horn-player")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
AUTH_HEADER = os.environ.get("AUTH_HEADER", "X-authentik-username")
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "true").lower() == "true"
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
DEFAULT_STATIONS = [
    ("Dead Air (Halloween)", "https://streaming.live365.com/a43471"),
    ("24/7 Christmas Music", "https://streaming.live365.com/a29903"),
]


class Hub:
    def __init__(self, protect, db, downmix):
        self.protect = protect
        self.db = db
        self.downmix = downmix
        self.players: dict[str, SpeakerPlayer] = {}
        self.speaker_error: str | None = None

    async def refresh_speakers(self):
        try:
            speakers = await self.protect.speakers()
            self.speaker_error = None
        except Exception as e:
            self.speaker_error = str(e)
            log.warning("speaker discovery: %s", e)
            return
        for s in speakers:
            if s["id"] in self.players:
                self.players[s["id"]].speaker.update(s)
            else:
                p = SpeakerPlayer(self.protect, s, self.downmix)
                p.start()
                self.players[s["id"]] = p

    async def discover_forever(self):
        while True:
            await self.refresh_speakers()
            await asyncio.sleep(300)

    def targets(self, speaker_ids):
        if speaker_ids in ("*", ["*"]):
            return list(self.players.values())
        ids = [speaker_ids] if isinstance(speaker_ids, str) else speaker_ids
        missing = [i for i in ids if i not in self.players]
        if missing:
            raise HTTPException(404, f"unknown speaker {missing[0]}")
        return [self.players[i] for i in ids]

    def source(self, source_id, kind=None):
        s = self.db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
        if not s or (kind and s["kind"] not in kind):
            raise HTTPException(404, "no such source")
        return s

    def play_source(self, speaker_ids, source_id, origin="manual"):
        s = self.source(source_id, ("station", "playlist"))
        for p in self.targets(speaker_ids):
            p.play_music(Music(s["id"], s["name"], s["kind"], s["target"], origin=origin))
        return s

    def stop_origin(self, origin):
        for p in self.players.values():
            p.stop(origin=origin)


hub: Hub = None
db: DB = None


@asynccontextmanager
async def lifespan(app):
    global hub, db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for d in ("music", "clips"):
        (DATA / d).mkdir(parents=True, exist_ok=True)
    db = DB(DATA / "horn-player.db")
    if not db.one("SELECT id FROM sources LIMIT 1"):
        for name, url in DEFAULT_STATIONS:
            db.x("INSERT INTO sources (kind, name, target) VALUES ('station', ?, ?)", (name, url))
    protect = Protect(os.environ["UNIFI_HOST"], os.environ["UNIFI_USER"], os.environ["UNIFI_PASS"])
    hub = Hub(protect, db, os.environ.get("DOWNMIX", "quad"))
    tasks = [asyncio.create_task(hub.discover_forever()), asyncio.create_task(Scheduler(db, hub).run())]
    yield
    for t in tasks:
        t.cancel()
    for p in hub.players.values():
        await p.shutdown()
    await protect.close()


app = FastAPI(title="horn-player", lifespan=lifespan)


@app.middleware("http")
async def auth(request: Request, call_next):
    user = request.headers.get(AUTH_HEADER)
    if REQUIRE_AUTH and not user and request.url.path != "/api/health":
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    request.state.user = user or "anonymous"
    return await call_next(request)


def audit(request, action):
    db.log(request.state.user, action)


# ---- state ----------------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/state")
async def state(request: Request):
    return {"user": request.state.user, "speaker_error": hub.speaker_error,
            "speakers": [p.state() for p in hub.players.values()]}


@app.post("/api/speakers/refresh")
async def refresh(request: Request):
    await hub.refresh_speakers()
    return await state(request)


@app.get("/api/activity")
async def activity():
    return db.q("SELECT * FROM activity ORDER BY id DESC LIMIT 100")


# ---- playback -------------------------------------------------------------------------------

class PlayReq(BaseModel):
    source_id: int


class TargetReq(BaseModel):
    speakers: list[str] = Field(min_length=1)


class ClipReq(TargetReq):
    clip_id: int


class SpeakReq(TargetReq):
    text: str = Field(min_length=1, max_length=500)
    voice: str = Field(default="piper", pattern="^(piper|protect)$")


@app.post("/api/speakers/{speaker_id}/play")
async def play(speaker_id: str, body: PlayReq, request: Request):
    s = hub.play_source(speaker_id, body.source_id)
    audit(request, f"played {s['name']} on {hub.players[speaker_id].speaker['name']}")
    return {"ok": True}


@app.post("/api/speakers/{speaker_id}/stop")
async def stop(speaker_id: str, request: Request):
    p = hub.targets(speaker_id)[0]
    p.stop()
    audit(request, f"stopped {p.speaker['name']}")
    return {"ok": True}


@app.post("/api/speakers/{speaker_id}/skip")
async def skip(speaker_id: str, request: Request):
    hub.targets(speaker_id)[0].skip()
    return {"ok": True}


@app.post("/api/soundboard")
async def soundboard(body: ClipReq, request: Request):
    c = hub.source(body.clip_id, ("clip",))
    players = hub.targets(body.speakers)
    for p in players:
        p.interrupt(Interrupt("clip", c["name"], c["target"]))
    audit(request, f"fired {c['name']} on {', '.join(p.speaker['name'] for p in players)}")
    return {"ok": True}


@app.post("/api/announce")
async def announce(body: SpeakReq, request: Request):
    players = hub.targets(body.speakers)
    if body.voice == "piper":
        try:
            item = ("tts", await synthesize(body.text, DATA / "tts-cache"))
        except RuntimeError as e:
            raise HTTPException(500, str(e))
    else:
        item = ("speak", body.text)
    results = await asyncio.gather(*(p.interrupt(Interrupt(item[0], "announcement", item[1]))
                                     for p in players), return_exceptions=True)
    errors = [str(r) for r in results if isinstance(r, Exception)]
    audit(request, f"announced '{body.text[:60]}'" + (f" (failed: {errors[0]})" if errors else ""))
    if errors:
        raise HTTPException(502, errors[0])
    return {"ok": True}


# ---- library --------------------------------------------------------------------------------

def _safe_name(name):
    name = re.sub(r"[^\w .()'-]", "", Path(name).name).strip(" .")
    if not name:
        raise HTTPException(400, "bad file name")
    return name


async def _save_upload(f: UploadFile, dest: Path):
    if Path(f.filename or "").suffix.lower() not in AUDIO_EXT:
        raise HTTPException(400, f"{f.filename}: not a supported audio file")
    size = 0
    with dest.open("wb") as out:
        while chunk := await f.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"{f.filename}: larger than {MAX_UPLOAD >> 20} MB")
            out.write(chunk)


class StationReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str

    @field_validator("url")
    @classmethod
    def http_only(cls, v):
        if not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("must be an http(s) URL")
        return v


class NameReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _insert(kind, name, target):
    if db.one("SELECT id FROM sources WHERE kind = ? AND name = ?", (kind, name)):
        raise HTTPException(409, f"a {kind} named '{name}' already exists")
    return db.x("INSERT INTO sources (kind, name, target) VALUES (?, ?, ?)", (kind, name, target))


@app.get("/api/sources")
async def sources():
    rows = db.q("SELECT * FROM sources ORDER BY kind, name")
    for r in rows:
        if r["kind"] == "playlist":
            r["tracks"] = sorted(p.name for p in Path(r["target"]).glob("*") if p.suffix.lower() in AUDIO_EXT)
        r.pop("target") if r["kind"] != "station" else None
    return rows


@app.post("/api/stations")
async def add_station(body: StationReq, request: Request):
    sid = _insert("station", body.name, body.url)
    audit(request, f"added station {body.name}")
    return {"id": sid}


@app.post("/api/playlists")
async def add_playlist(body: NameReq, request: Request):
    sid = _insert("playlist", body.name, "")
    d = DATA / "music" / str(sid)
    d.mkdir(parents=True, exist_ok=True)
    db.x("UPDATE sources SET target = ? WHERE id = ?", (str(d), sid))
    audit(request, f"created playlist {body.name}")
    return {"id": sid}


@app.post("/api/playlists/{source_id}/tracks")
async def upload_tracks(source_id: int, request: Request, files: list[UploadFile] = File(...)):
    s = hub.source(source_id, ("playlist",))
    for f in files:
        await _save_upload(f, Path(s["target"]) / _safe_name(f.filename))
    audit(request, f"uploaded {len(files)} track(s) to {s['name']}")
    return {"ok": True}


@app.delete("/api/playlists/{source_id}/tracks/{track}")
async def delete_track(source_id: int, track: str, request: Request):
    s = hub.source(source_id, ("playlist",))
    (Path(s["target"]) / _safe_name(track)).unlink(missing_ok=True)
    audit(request, f"removed {track} from {s['name']}")
    return {"ok": True}


@app.post("/api/clips")
async def add_clip(request: Request, name: str = Form(..., min_length=1, max_length=80),
                   file: UploadFile = File(...)):
    sid = _insert("clip", name, "")
    dest = DATA / "clips" / f"{sid}{Path(file.filename or '').suffix.lower()}"
    try:
        await _save_upload(file, dest)
    except HTTPException:
        db.x("DELETE FROM sources WHERE id = ?", (sid,))
        raise
    db.x("UPDATE sources SET target = ? WHERE id = ?", (str(dest), sid))
    audit(request, f"added clip {name}")
    return {"id": sid}


@app.get("/api/clips/{source_id}/audio")
async def clip_audio(source_id: int):
    return FileResponse(hub.source(source_id, ("clip",))["target"])


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: int, request: Request):
    s = hub.source(source_id)
    for p in hub.players.values():
        if p.music and p.music.source_id == source_id:
            p.stop()
    db.x("DELETE FROM sources WHERE id = ?", (source_id,))
    if s["kind"] == "playlist":
        shutil.rmtree(s["target"], ignore_errors=True)
    elif s["kind"] == "clip":
        Path(s["target"]).unlink(missing_ok=True)
    audit(request, f"deleted {s['kind']} {s['name']}")
    return {"ok": True}


# ---- schedules ------------------------------------------------------------------------------

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE = r"^\d{4}-\d{2}-\d{2}$"


class ScheduleReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    speaker_id: str
    source_id: int
    days: str = Field(default="0123456", pattern=r"^[0-6]{1,7}$")
    start_time: str = Field(pattern=HHMM)
    end_time: str = Field(pattern=HHMM)
    start_date: str | None = Field(default=None, pattern=DATE)
    end_date: str | None = Field(default=None, pattern=DATE)
    enabled: bool = True


def _check_schedule(body: ScheduleReq):
    hub.source(body.source_id, ("station", "playlist"))
    if body.speaker_id != "*":
        hub.targets(body.speaker_id)
    if body.start_time == body.end_time:
        raise HTTPException(400, "start and end time are the same")


FIELDS = ("name", "speaker_id", "source_id", "days", "start_time", "end_time", "start_date", "end_date", "enabled")


@app.get("/api/schedules")
async def schedules():
    return db.q("SELECT * FROM schedules ORDER BY start_time")


@app.post("/api/schedules")
async def add_schedule(body: ScheduleReq, request: Request):
    _check_schedule(body)
    sid = db.x(f"INSERT INTO schedules ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
               tuple(getattr(body, f) for f in FIELDS))
    audit(request, f"created schedule {body.name}")
    return {"id": sid}


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: ScheduleReq, request: Request):
    _check_schedule(body)
    if not db.one("SELECT id FROM schedules WHERE id = ?", (schedule_id,)):
        raise HTTPException(404, "no such schedule")
    db.x(f"UPDATE schedules SET {', '.join(f + ' = ?' for f in FIELDS)} WHERE id = ?",
         tuple(getattr(body, f) for f in FIELDS) + (schedule_id,))
    audit(request, f"updated schedule {body.name}")
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, request: Request):
    db.x("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    audit(request, f"deleted schedule {schedule_id}")
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
