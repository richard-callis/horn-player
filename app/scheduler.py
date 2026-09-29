"""Start and stop music on speakers according to the schedules table."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

RETRY_AFTER_FAILURE = 60  # seconds before re-asserting a schedule on a speaker that failed hard


def is_active(s, now: datetime):
    """Whether schedule row s covers `now` (local time). Windows may cross midnight."""
    if not s["enabled"]:
        return False
    hm = now.strftime("%H:%M")
    start, end = s["start_time"], s["end_time"]
    if start <= end:
        if not (start <= hm < end):
            return False
        day = now
    elif hm >= start:
        day = now                          # evening part of an overnight window
    elif hm < end:
        day = now - timedelta(days=1)      # after-midnight part belongs to yesterday's window
    else:
        return False
    d = day.strftime("%Y-%m-%d")
    if s["start_date"] and d < s["start_date"]:
        return False
    if s["end_date"] and d > s["end_date"]:
        return False
    return str(day.weekday()) in s["days"]


class Scheduler:
    """Each tick: stop schedules whose window closed, start newly opened (or edited) ones on every
    target speaker, and re-start an open window on a speaker that went quiet on its own, e.g.
    after a restart or an error, but not one somebody stopped by hand during the window."""

    def __init__(self, db, hub, interval=15):
        self.db = db
        self.hub = hub
        self.interval = interval
        self.open: dict[int, tuple] = {}   # schedule id -> (window opened at, (speaker_id, source_id))

    async def run(self):
        await self.hub.ready.wait()        # don't evaluate schedules before speakers are known
        while True:
            try:
                await self.tick(datetime.now())
            except Exception:
                log.exception("scheduler tick")
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime, wall=None):
        wall = wall if wall is not None else time.time()
        rows = self.db.q("SELECT * FROM schedules")
        active = {s["id"]: s for s in rows if is_active(s, now)}
        for sid in list(self.open):
            if sid not in active:
                self.hub.stop_origin(f"schedule:{sid}")
                del self.open[sid]
                self.db.log("scheduler", f"schedule {sid} ended")
        for sid, s in active.items():
            origin = f"schedule:{sid}"
            key = (s["speaker_id"], s["source_id"])
            fresh = sid not in self.open or self.open[sid][1] != key
            if fresh:
                if sid in self.open:          # edited while open: replace what the old version started
                    self.hub.stop_origin(origin)
                self.open[sid] = (wall, key)
                self.db.log("scheduler", f"schedule '{s['name']}' started")
            opened = self.open[sid][0]
            for p in self.hub.speakers_for(s["speaker_id"]):
                if p.music and p.music.origin == origin:
                    continue
                if not fresh and (p.music or p.last_user_stop >= opened
                                  or wall - p.last_failure < RETRY_AFTER_FAILURE):
                    continue
                try:
                    self.hub.start(p, s["source_id"], origin)
                except LookupError as e:
                    log.warning("schedule %s: %s", sid, e)
