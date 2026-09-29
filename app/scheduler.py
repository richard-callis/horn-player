"""Start and stop music on speakers according to the schedules table."""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


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
    def __init__(self, db, hub, interval=15):
        self.db = db
        self.hub = hub
        self.interval = interval
        self.active: set[int] = set()

    async def run(self):
        while True:
            try:
                await self.tick(datetime.now())
            except Exception:
                log.exception("scheduler tick")
            await asyncio.sleep(self.interval)

    async def tick(self, now):
        rows = self.db.q("SELECT * FROM schedules")
        now_active = {s["id"]: s for s in rows if is_active(s, now)}
        for sid in self.active - now_active.keys():
            self.hub.stop_origin(f"schedule:{sid}")
            self.db.log("scheduler", f"schedule {sid} ended")
        for sid, s in now_active.items():
            if sid not in self.active:
                try:
                    self.hub.play_source(s["speaker_id"], s["source_id"], origin=f"schedule:{sid}")
                    self.db.log("scheduler", f"schedule '{s['name']}' started")
                except Exception as e:
                    log.warning("schedule %s: %s", sid, e)
                    self.db.log("scheduler", f"schedule '{s['name']}' failed: {e}")
        self.active = set(now_active)
