"""SQLite storage. Raw Withings data is kept as-is; derived metrics are cached separately."""
import json
import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT, refresh_token TEXT, expires_at INTEGER, userid TEXT, scope TEXT
);
-- raw sensor series
CREATE TABLE IF NOT EXISTS hr (ts INTEGER PRIMARY KEY, bpm INTEGER NOT NULL, src TEXT);
CREATE TABLE IF NOT EXISTS steps (
    ts INTEGER PRIMARY KEY, steps INTEGER, duration INTEGER, calories REAL, distance REAL, elevation REAL
);
CREATE TABLE IF NOT EXISTS spo2 (ts INTEGER PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS sleep_series (
    ts INTEGER PRIMARY KEY, rr REAL, snoring REAL, rmssd REAL, sdnn REAL, mvt REAL
);
CREATE TABLE IF NOT EXISTS sleep_states (start INTEGER PRIMARY KEY, end INTEGER, state INTEGER);
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id INTEGER PRIMARY KEY, date TEXT, start INTEGER, end INTEGER, tz TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS sleep_sessions_date ON sleep_sessions(date);
CREATE TABLE IF NOT EXISTS activity_daily (date TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY, category INTEGER, start INTEGER, end INTEGER, date TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS measures (
    grpid INTEGER, type INTEGER, ts INTEGER, value REAL, PRIMARY KEY (grpid, type)
);
CREATE INDEX IF NOT EXISTS measures_type_ts ON measures(type, ts);
-- personal journal
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
    mood INTEGER, energy INTEGER, tags TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts);
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, emoji TEXT, sort INTEGER DEFAULT 0);
-- derived
CREATE TABLE IF NOT EXISTS daily_metrics (date TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS intraday_derived (ts INTEGER PRIMARY KEY, stress INTEGER, energy REAL);
"""

DEFAULT_TAGS = [
    ("tired", "😴"), ("headache", "🤕"), ("alcohol", "🍷"), ("little sleep", "🥱"),
    ("late caffeine", "☕"), ("stressed", "😰"), ("sick", "🤒"), ("late meal", "🍕"),
    ("great workout", "💪"), ("sore", "🦵"), ("relaxed", "🧘"), ("screen late", "📱"),
]

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            if _conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0:
                _conn.executemany(
                    "INSERT INTO tags(name, emoji, sort) VALUES (?,?,?)",
                    [(n, e, i) for i, (n, e) in enumerate(DEFAULT_TAGS)],
                )
        return _conn


def reset_connection(path=None):
    """Used by tests / demo to point at another database file."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        if path is not None:
            config.DB_PATH = path


@contextmanager
def tx():
    c = conn()
    with _lock:
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql: str, args=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args=()):
    with _lock:
        return conn().execute(sql, args).fetchone()


def kv_get(key: str, default=None):
    row = q1("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(row[0]) if row else default


def kv_set(key: str, value) -> None:
    with _lock:
        conn().execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, json.dumps(value)))
