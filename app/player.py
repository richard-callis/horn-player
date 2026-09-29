"""Per-speaker playback engine.

Each speaker has one background task that owns its talkback socket. "Music" (a station or a
playlist) is the long-running program; a clip or an announcement interrupts it and the music
resumes afterwards (playlists pick up the same track where they left off).
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import websockets

from . import audio
from .icy import now_playing
from .protect import TalkbackRefused

log = logging.getLogger(__name__)

LEAD = 0.40              # run this far ahead of real time to keep the speaker's jitter buffer full
IDLE_CLOSE = 5.0         # close the talkback socket after this long with nothing to play
POLL = 0.25              # how often a stalled source re-checks for stop/interrupt requests
# Seconds of a track the listener hasn't actually heard when a stream is cut: the 0.9 s lead-in
# silence plus LEAD, plus a second of rewind so the resume overlaps slightly.
RESUME_REWIND = 0.9 + LEAD + 1.0
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma", ".webm"}


@dataclass
class Music:
    source_id: int
    name: str
    kind: str                     # "station" | "playlist"
    target: str                   # stream URL or playlist directory
    origin: str = "manual"        # "manual" or "schedule:<id>"
    shuffle: bool = True
    tracks: list = field(default_factory=list)
    index: int = 0
    offset: float = 0.0           # seconds into the current track (for resume after an interruption)
    failures: int = 0             # consecutive tracks that failed to play

    def current(self):
        if self.kind == "station":
            return self.target
        if not self.tracks or self.index >= len(self.tracks):
            files = sorted(str(p) for p in Path(self.target).rglob("*") if p.suffix.lower() in AUDIO_EXT)
            if not files:
                raise PlaylistEmpty(f"playlist '{self.name}' has no audio files")
            if self.shuffle:
                random.shuffle(files)
            self.tracks, self.index, self.offset = files, 0, 0.0
        return self.tracks[self.index]


class PlaylistEmpty(Exception):
    pass


@dataclass
class Interrupt:
    kind: str                     # "clip" | "tts" (a rendered file) | "speak" (Protect's own voice)
    name: str
    target: str                   # audio file path, or text for "speak"
    done: asyncio.Future = None


class SpeakerPlayer:
    def __init__(self, protect, speaker, downmix="quad"):
        self.protect = protect
        self.speaker = speaker            # dict with id, mac, name
        self.downmix = downmix
        self.music: Music | None = None
        self.queue: list[Interrupt] = []
        self.playing: str | None = None   # human-readable description of what is sounding now
        self.title: str | None = None     # now-playing song title
        self.error: str | None = None
        self.last_user_stop = 0.0         # time.time() of the last manual stop (the scheduler respects it)
        self.last_failure = 0.0           # time.time() of the last fatal failure
        self._wake = asyncio.Event()
        self._preempt = False
        self._abort = False
        self._ws = None
        self._task = None
        self._title_task = None

    # ---- commands -------------------------------------------------------------------------

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"player-{self.speaker['id']}")

    async def shutdown(self):
        for t in (self._task, self._title_task):
            if t:
                t.cancel()
        await self._close_ws()

    def play_music(self, music: Music):
        self.music = music
        self.error = None
        self._kick()

    def stop(self, origin=None):
        """Stop the music (only if it came from `origin`, when given) and, for a manual stop,
        anything queued too."""
        if origin and (not self.music or self.music.origin != origin):
            return False
        self.music = None
        if not origin:
            self.last_user_stop = time.time()
            self.error = None
            for it in self.queue:
                if it.done and not it.done.done():
                    it.done.set_result(False)
            self.queue.clear()
            self._abort = True
        self._kick()
        return True

    def interrupt(self, item: Interrupt):
        item.done = asyncio.get_running_loop().create_future()
        self.queue.append(item)
        self._kick()
        return item.done

    def skip(self):
        if self.music and self.music.kind == "playlist":
            self.music.index += 1
            self.music.offset = 0.0
            self._kick()

    def state(self):
        m = self.music
        return {
            "id": self.speaker["id"], "name": self.speaker["name"], "state": self.speaker.get("state"),
            "playing": self.playing, "title": self.title, "error": self.error,
            "music": m and {"source_id": m.source_id, "name": m.name, "kind": m.kind, "origin": m.origin},
            "queued": [i.name for i in self.queue],
        }

    def _kick(self):
        self._preempt = True
        self._wake.set()

    # ---- engine ---------------------------------------------------------------------------

    async def _run(self):
        backoff = 0
        while True:
            self._wake.clear()
            self._preempt = self._abort = False
            try:
                if self.queue:
                    await self._next_interrupt()
                    continue
                if self.music:
                    await self._play_music(self.music)
                    backoff = 0
                    continue
            except asyncio.CancelledError:
                raise
            except TalkbackRefused as e:
                self._fail(str(e))
            except PlaylistEmpty as e:
                self._fail(str(e))
            except Exception as e:
                # Protect, network or stream trouble: keep the music and retry with backoff.
                log.warning("speaker %s: %s", self.speaker["name"], e)
                self.error = str(e)
                await self._close_ws()
                backoff = min(60, max(3, backoff * 2))
                await self._sleep(backoff)
                continue
            self.playing = None
            self._set_title(None)
            try:
                await asyncio.wait_for(self._wake.wait(), IDLE_CLOSE)
            except asyncio.TimeoutError:
                await self._close_ws()
                await self._wake.wait()

    async def _sleep(self, seconds):
        """Sleep, but wake early for a new command."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _fail(self, msg):
        self.error = msg
        self.music = None
        self.last_failure = time.time()
        for it in self.queue:
            if it.done and not it.done.done():
                it.done.set_exception(RuntimeError(msg))
        self.queue.clear()

    async def _next_interrupt(self):
        item = self.queue[0]
        try:
            await self._play_interrupt(item)
        except (PermissionError, RuntimeError) as e:
            # One failed clip/announcement shouldn't kill the music underneath it.
            self.error = str(e)
            if item.done and not item.done.done():
                item.done.set_exception(e)
        finally:
            if self.queue and self.queue[0] is item:
                self.queue.pop(0)

    async def _play_interrupt(self, item: Interrupt):
        self._preempt = False
        self.playing = f"{item.kind if item.kind == 'clip' else 'announcement'}: {item.name}"
        self._set_title(None)
        if item.kind in ("clip", "tts"):
            await self._stream(item.target, stop_on_preempt=False)
        else:
            await self._close_ws()  # let the speaker's own TTS have the audio path
            await self.protect.speak(self.speaker["mac"], item.target)
            await self._sleep(2 + len(item.target.split()) / 2.5)  # rough speaking time
        if item.done and not item.done.done():
            item.done.set_result(True)

    async def _play_music(self, m: Music):
        track = m.current()
        idx = m.index
        self.playing = m.name
        self._watch_title(m, track)
        if m.kind == "station":
            await self._stream(track)
            if not self._preempt and self.music is m:
                await self._sleep(2)  # the stream ended on its own; reconnect
            self.error = None if self.music is m else self.error
            return
        started = time.monotonic()
        try:
            finished = await self._stream(track, seek=m.offset)
        except audio.SourceError as e:
            # A broken file: skip it, and give up once every track in a row has failed.
            log.warning("speaker %s: %s", self.speaker["name"], e)
            self.error = str(e)
            m.failures += 1
            if m.failures >= len(m.tracks):
                raise PlaylistEmpty(f"no playable tracks in '{m.name}'") from None
            m.index, m.offset = idx + 1, 0.0
            return
        m.failures = 0
        if self.music is not m or m.index != idx:
            return                          # replaced, stopped, or skipped meanwhile
        if finished:
            m.index, m.offset = idx + 1, 0.0
        else:
            elapsed = time.monotonic() - started
            if elapsed > RESUME_REWIND:
                m.offset += elapsed - RESUME_REWIND

    def _set_title(self, title):
        if self._title_task:
            self._title_task.cancel()
            self._title_task = None
        self.title = title

    def _watch_title(self, m, track):
        self._set_title(None)

        async def watch():
            if m.kind == "playlist":
                self.title = await audio.probe_title(track) or Path(track).stem
                return
            while self.music is m:
                try:
                    self.title = await now_playing(track)
                except Exception:
                    pass
                await asyncio.sleep(30)

        self._title_task = asyncio.create_task(watch())

    async def _stream(self, src, seek=0.0, stop_on_preempt=True):
        """Pace src's frames to the speaker; True if it played to the end, False if preempted.
        Stop/interrupt requests are honoured within POLL seconds even while the source stalls."""
        t0, n = None, 0
        gen = audio.adts_frames(src, self.downmix, seek)
        pending = None
        try:
            while True:
                if self._abort or (stop_on_preempt and self._preempt):
                    return False
                if pending is None:
                    pending = asyncio.ensure_future(gen.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=POLL)
                if not done:
                    continue
                task, pending = pending, None
                try:
                    frame = task.result()
                except StopAsyncIteration:
                    return True
                if self._ws is None:
                    self._ws = await self.protect.open_talkback(self.speaker["id"])
                    self.error = None
                if t0 is None:
                    t0 = time.monotonic()
                delay = t0 + n * audio.FRAME_DT - LEAD - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -2:          # source stalled (radio rebuffer); resync the clock
                    t0, n = time.monotonic() + LEAD, 0
                try:
                    await self._ws.send(frame)
                except websockets.ConnectionClosed as e:
                    self._ws = None
                    code = e.rcvd.code if e.rcvd else None
                    if code == 4403:
                        raise TalkbackRefused(code) from None
                    log.warning("talkback closed (%s), reconnecting", code)
                n += 1
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await gen.aclose()

    async def _close_ws(self):
        if self._ws is not None:
            ws, self._ws = self._ws, None
            try:
                await ws.close()
            except Exception:
                pass
