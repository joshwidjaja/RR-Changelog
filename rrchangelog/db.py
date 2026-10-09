"""SQLite storage. Timestamps are ISO-8601 UTC strings; one connection per thread."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fictions (
    id              INTEGER PRIMARY KEY,
    rr_fiction_id   INTEGER NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL,
    cover_url       TEXT NOT NULL DEFAULT '',
    added_at        TEXT NOT NULL,
    last_synced_at  TEXT                -- NULL until the first successful sync
);

CREATE TABLE IF NOT EXISTS chapters (
    id               INTEGER PRIMARY KEY,
    fiction_id       INTEGER NOT NULL REFERENCES fictions(id) ON DELETE CASCADE,
    rr_chapter_id    INTEGER NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    position         INTEGER NOT NULL,
    published_at     TEXT,
    edited_at        TEXT,              -- NULL = never edited (RoyalRoad shows no edit date)
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'removed')),
    first_seen_at    TEXT NOT NULL,
    last_checked_at  TEXT,
    check_error      TEXT
);
CREATE INDEX IF NOT EXISTS chapters_fiction ON chapters (fiction_id, position);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                INTEGER PRIMARY KEY,
    fiction_id        INTEGER NOT NULL REFERENCES fictions(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'done', 'failed', 'interrupted')),
    queued_at         TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    chapters_total    INTEGER NOT NULL DEFAULT 0,
    chapters_checked  INTEGER NOT NULL DEFAULT 0,
    new_count         INTEGER NOT NULL DEFAULT 0,
    edited_count      INTEGER NOT NULL DEFAULT 0,
    removed_count     INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS sync_runs_fiction ON sync_runs (fiction_id, id);

CREATE TABLE IF NOT EXISTS chapter_changes (
    id           INTEGER PRIMARY KEY,
    chapter_id   INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    sync_run_id  INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    change_type  TEXT NOT NULL
                 CHECK (change_type IN ('new', 'edited', 'removed', 'restored', 'retitled', 'unlocked')),
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chapter_changes_run ON chapter_changes (sync_run_id);
"""

ACTIVE_RUN_STATUSES = ("queued", "running")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # lets the API read while the worker writes
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def active_run(conn: sqlite3.Connection, fiction_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_runs WHERE fiction_id = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
        (fiction_id, *ACTIVE_RUN_STATUSES),
    ).fetchone()


def create_run(conn: sqlite3.Connection, fiction_id: int) -> int:
    cur = conn.execute("INSERT INTO sync_runs (fiction_id, queued_at) VALUES (?, ?)", (fiction_id, now()))
    conn.commit()
    return cur.lastrowid
