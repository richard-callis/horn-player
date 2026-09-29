"""SQLite state: library sources, schedules, activity log."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('station', 'playlist', 'clip')),
    name TEXT NOT NULL,
    target TEXT NOT NULL,          -- stream URL, playlist directory, or clip file
    UNIQUE (kind, name)
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    speaker_id TEXT NOT NULL,      -- a Protect speaker id, or '*' for all speakers
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    days TEXT NOT NULL DEFAULT '0123456',   -- weekdays, Monday = 0
    start_time TEXT NOT NULL,      -- HH:MM
    end_time TEXT NOT NULL,        -- HH:MM; earlier than start_time means it runs past midnight
    start_date TEXT,               -- YYYY-MM-DD, optional
    end_date TEXT,                 -- YYYY-MM-DD, optional
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    user TEXT,
    action TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self._lock, self._conn:
            return self._conn.execute(sql, args).lastrowid

    def log(self, user, action):
        self.x("INSERT INTO activity (ts, user, action) VALUES (?, ?, ?)", (time.time(), user, action))
        self.x("DELETE FROM activity WHERE id <= (SELECT MAX(id) - 1000 FROM activity)")
