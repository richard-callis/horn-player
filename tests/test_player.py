"""Player and scheduler behaviour against a fake Protect and fake audio sources."""
import asyncio
import time
from datetime import datetime

import pytest

from app import audio, player
from app.player import Interrupt, Music, SpeakerPlayer
from app.scheduler import Scheduler

pytestmark = pytest.mark.asyncio


class FakeWS:
    def __init__(self):
        self.sent = 0

    async def send(self, frame):
        self.sent += 1

    async def close(self):
        pass


class FakeProtect:
    def __init__(self):
        self.ws = FakeWS()

    async def open_talkback(self, speaker_id):
        return self.ws


def fake_source(frames_by_src, stall=()):
    """adts_frames stand-in: n frames per source; sources in `stall` hang forever after one frame;
    sources mapped to None fail like an unreadable file."""
    seeks = []

    async def gen(src, downmix="quad", seek=0.0, lead_silence_ms=900):
        seeks.append((src, seek))
        n = frames_by_src.get(src, 5)
        if n is None:
            raise audio.SourceError(f"can't play {src}")
        for i in range(n):
            if src in stall and i == 1:
                await asyncio.Event().wait()
            yield b"f"

    return gen, seeks


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(player, "LEAD", 10.0)          # never sleep for pacing in tests
    monkeypatch.setattr(player, "POLL", 0.01)

    async def no_title(url):
        return None
    monkeypatch.setattr(player, "now_playing", no_title)
    monkeypatch.setattr(audio, "probe_title", no_title)


def make_player():
    p = SpeakerPlayer(FakeProtect(), {"id": "s1", "mac": "m", "name": "Horn"})
    p.start()
    return p


def playlist(tracks):
    m = Music(1, "list", "playlist", "/nowhere")
    m.tracks, m.index = list(tracks), 0
    return m


async def until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


async def test_skip_starts_next_track_from_the_top(monkeypatch):
    gen, seeks = fake_source({"a": 10_000, "b": 10_000, "c": 10_000})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = make_player()
    m = playlist(["a", "b", "c"])
    m.offset = 0.0
    p.play_music(m)
    await until(lambda: seeks)
    await asyncio.sleep(0.05)
    p.skip()
    await until(lambda: len(seeks) >= 2)
    assert seeks[1] == ("b", 0.0)        # not seeked by the time spent in "a"
    assert m.index == 1
    await p.shutdown()


async def test_stop_is_honoured_while_source_stalls(monkeypatch):
    gen, _ = fake_source({"http://radio": 100}, stall={"http://radio"})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = make_player()
    p.play_music(Music(1, "radio", "station", "http://radio"))
    await until(lambda: p.protect.ws.sent == 1)
    p.stop()
    await until(lambda: p.playing is None)
    await p.shutdown()


async def test_clip_interrupts_stalled_station_then_music_resumes(monkeypatch):
    gen, seeks = fake_source({"http://radio": 100, "clip.mp3": 3}, stall={"http://radio"})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = make_player()
    p.play_music(Music(1, "radio", "station", "http://radio"))
    await until(lambda: p.protect.ws.sent == 1)
    done = p.interrupt(Interrupt("clip", "siren", "clip.mp3"))
    assert await asyncio.wait_for(done, 2) is True
    await until(lambda: [s for s, _ in seeks].count("http://radio") == 2)
    assert p.music is not None
    await p.shutdown()


async def test_broken_track_is_skipped_not_fatal(monkeypatch):
    gen, seeks = fake_source({"bad": None, "good": 10_000})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = make_player()
    m = playlist(["bad", "good"])
    p.play_music(m)
    await until(lambda: ("good", 0.0) in seeks)
    assert p.music is m
    await p.shutdown()


async def test_all_tracks_broken_gives_up(monkeypatch):
    gen, _ = fake_source({"x": None, "y": None})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = make_player()
    p.play_music(playlist(["x", "y"]))
    await until(lambda: p.music is None)
    assert "no playable tracks" in p.error
    await p.shutdown()


# ---- scheduler ------------------------------------------------------------------------------

class FakeDB:
    def __init__(self, schedules):
        self.schedules = schedules

    def q(self, sql, args=()):
        return self.schedules

    def log(self, user, action):
        pass


class FakePlayer:
    def __init__(self, pid):
        self.speaker = {"id": pid}
        self.music = None
        self.last_user_stop = 0.0
        self.last_failure = 0.0


class FakeHub:
    def __init__(self, players):
        self.players = {p.speaker["id"]: p for p in players}
        self.started = []

    def speakers_for(self, sid):
        return list(self.players.values()) if sid == "*" else [self.players[sid]] if sid in self.players else []

    def start(self, p, source_id, origin):
        p.music = Music(source_id, "src", "station", "u", origin=origin)
        self.started.append((p.speaker["id"], source_id))

    def stop_origin(self, origin):
        for p in self.players.values():
            if p.music and p.music.origin == origin:
                p.music = None


def sched(**kw):
    s = {"id": 1, "name": "evening", "enabled": 1, "days": "0123456", "start_time": "17:00",
         "end_time": "21:00", "start_date": None, "end_date": None, "speaker_id": "*", "source_id": 7}
    s.update(kw)
    return s


EVENING = datetime(2026, 10, 31, 18, 0)


async def test_schedule_waits_for_speakers_then_starts():
    hub = FakeHub([])
    sc = Scheduler(FakeDB([sched()]), hub)
    await sc.tick(EVENING, wall=1000)
    assert hub.started == []                       # no speakers known yet: nothing lost
    hub.players["s1"] = FakePlayer("s1")
    await sc.tick(EVENING, wall=1015)
    assert hub.started == [("s1", 7)]              # picked up once the speaker appears


async def test_reasserts_after_failure_but_not_after_manual_stop():
    p = FakePlayer("s1")
    hub = FakeHub([p])
    sc = Scheduler(FakeDB([sched()]), hub)
    await sc.tick(EVENING, wall=1000)
    p.music, p.last_failure = None, 1001           # died on its own
    await sc.tick(EVENING, wall=1030)
    assert len(hub.started) == 1                   # still inside the retry delay
    await sc.tick(EVENING, wall=1100)
    assert len(hub.started) == 2                   # re-started
    p.music, p.last_user_stop = None, 1200         # somebody pressed stop
    await sc.tick(EVENING, wall=1300)
    assert len(hub.started) == 2                   # respected for the rest of the window


async def test_editing_an_open_schedule_applies_immediately():
    p = FakePlayer("s1")
    db = FakeDB([sched()])
    hub = FakeHub([p])
    sc = Scheduler(db, hub)
    await sc.tick(EVENING, wall=1000)
    db.schedules = [sched(source_id=8)]
    await sc.tick(EVENING, wall=1015)
    assert hub.started[-1] == ("s1", 8) and p.music.source_id == 8


async def test_window_close_stops_only_its_own_music():
    p, q = FakePlayer("s1"), FakePlayer("s2")
    hub = FakeHub([p, q])
    sc = Scheduler(FakeDB([sched(speaker_id="s1")]), hub)
    await sc.tick(EVENING, wall=1000)
    q.music = Music(3, "manual", "station", "u")
    await sc.tick(datetime(2026, 10, 31, 21, 30), wall=2000)
    assert p.music is None and q.music is not None


async def test_refused_announcement_answers_instead_of_hanging(monkeypatch):
    from app.protect import TalkbackRefused

    class Refusing(FakeProtect):
        async def open_talkback(self, speaker_id):
            raise TalkbackRefused(4403)

    gen, _ = fake_source({"say.wav": 3})
    monkeypatch.setattr(audio, "adts_frames", gen)
    p = SpeakerPlayer(Refusing(), {"id": "s1", "mac": "m", "name": "Horn"})
    p.start()
    done = p.interrupt(Interrupt("tts", "hi", "say.wav"))
    with pytest.raises(TalkbackRefused):
        await asyncio.wait_for(done, 2)
    await p.shutdown()
