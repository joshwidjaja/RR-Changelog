"""
RR-Changelog — SQLite storage layer.

Deliberately written against the stdlib `sqlite3` module with no ORM. At three
tables an ORM would be pure overhead, and the point here is to learn SQL rather
than to learn an abstraction over it. The comments are heavier than production
code would carry, for the same reason.

Timestamps are stored as ISO-8601 UTC strings ('2026-08-10T03:37:11.429515+00:00').
SQLite has no date type, so the choice is text or integer epoch. Text wins because
it sorts lexicographically in the same order it sorts chronologically, and it's
readable when you open the file in a DB browser.
"""

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fictions (
    id                INTEGER PRIMARY KEY,
    rr_id             INTEGER NOT NULL UNIQUE,
    title             TEXT    NOT NULL,
    author            TEXT,
    url               TEXT    NOT NULL,
    added_at          TEXT    NOT NULL,
    last_refreshed_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
    id              INTEGER PRIMARY KEY,
    fiction_id      INTEGER NOT NULL REFERENCES fictions(id) ON DELETE CASCADE,
    rr_chapter_id   INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    position        INTEGER NOT NULL,
    published_at    TEXT,
    -- NULL here is MEANINGFUL: RoyalRoad omits the "Last Edit" row entirely for a
    -- chapter that has never been edited. So NULL means "never edited", not
    -- "unknown". Never COALESCE this away in storage -- only in queries.
    updated_at      TEXT,
    content_hash    TEXT,
    word_count      INTEGER,
    first_seen_at   TEXT    NOT NULL,
    last_checked_at TEXT,
    last_read_at    TEXT,
    UNIQUE (fiction_id, rr_chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_chapters_fiction ON chapters(fiction_id);

CREATE TABLE IF NOT EXISTS revisions (
    id             INTEGER PRIMARY KEY,
    chapter_id     INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    observed_at    TEXT NOT NULL,
    old_updated_at TEXT,
    new_updated_at TEXT,
    old_hash       TEXT,
    new_hash       TEXT,
    word_delta     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_revisions_chapter ON revisions(chapter_id);
"""

# Sorting expression used everywhere a chapter list is ordered by recency.
# An unedited chapter has updated_at IS NULL, and NULL sorts before every string
# in SQLite -- so ordering on the raw column would bury never-edited chapters at
# one end regardless of how recently they were published. COALESCE gives each row
# its effective "last touched" moment while leaving the column itself untouched.
EFFECTIVE_UPDATED = "COALESCE(updated_at, published_at)"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def to_iso(value):
    """Accept a datetime or an already-ISO string; store one consistent shape."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def connect(path="rrchangelog.db"):
    """
    Open a connection with the pragmas this project depends on.

    Both pragmas are per-connection, not per-database -- setting them once at
    creation time does nothing for later connections. That is why this helper
    exists and why nothing should call sqlite3.connect() directly.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row          # rows behave like dicts, not tuples
    conn.execute("PRAGMA foreign_keys = ON")   # OFF by default; REFERENCES is
                                               # silently inert without this
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block on a writer,
                                               # so `list` works during `refresh`
    return conn


def init(conn):
    """Create the schema. Safe to call repeatedly."""
    with conn:                    # `with conn:` = one transaction, auto-commit
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def upsert_fiction(conn, rr_id, title, url, author=None):
    """
    Insert, or update the mutable fields if we've seen this fiction before.

    ON CONFLICT needs a uniqueness constraint to conflict *on* -- here the
    UNIQUE on rr_id. `excluded` is SQLite's name for the row that failed to
    insert, so `excluded.title` is the new title.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO fictions (rr_id, title, author, url, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(rr_id) DO UPDATE SET
                title  = excluded.title,
                author = COALESCE(excluded.author, fictions.author),
                url    = excluded.url
            """,
            (rr_id, title, author, url, now_iso()),
        )
    row = conn.execute("SELECT id FROM fictions WHERE rr_id = ?", (rr_id,)).fetchone()
    return row["id"]


def upsert_chapter(conn, fiction_id, ch):
    """
    Insert or update one chapter, and log a revision when something moved.

    `ch` is the dict produced by the page extractor. Returns one of:
        "new"        -- first time we've seen this chapter at all
        "baseline"   -- first time we've actually READ its chapter page
        "edited"     -- the Last Edit timestamp or the content hash moved
        "retitled"   -- title changed but content did not
        "unchanged"

    "baseline" exists because `add` seeds rows from the table of contents, which
    carries publication dates but no edit information and no content. The first
    refresh therefore fills in updated_at and content_hash for every chapter --
    and treating that as a revision would write one bogus revision row per
    chapter on the first refresh of every fiction, burying the real changelog.
    Discovering that a chapter was edited some time before you started tracking
    is not the same event as watching it change.

    The whole thing runs inside a single `with conn:` block so the chapter row
    and its revision row commit together or not at all. Per-chapter commits (as
    opposed to one giant transaction around a 300-chapter refresh) are what make
    an interrupted refresh resumable -- everything already fetched stays saved.
    """
    updated_at = to_iso(ch.get("updated_at"))
    published_at = to_iso(ch.get("published_at"))
    ts = now_iso()

    prior = conn.execute(
        "SELECT * FROM chapters WHERE fiction_id = ? AND rr_chapter_id = ?",
        (fiction_id, ch["chapter_id"]),
    ).fetchone()

    with conn:
        conn.execute(
            """
            INSERT INTO chapters (
                fiction_id, rr_chapter_id, title, url, position,
                published_at, updated_at, content_hash, word_count,
                first_seen_at, last_checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fiction_id, rr_chapter_id) DO UPDATE SET
                title           = excluded.title,
                url             = excluded.url,
                position        = excluded.position,
                published_at    = excluded.published_at,
                updated_at      = excluded.updated_at,
                content_hash    = excluded.content_hash,
                word_count      = excluded.word_count,
                last_checked_at = excluded.last_checked_at
            """,
            (
                fiction_id, ch["chapter_id"], ch["title"], ch["url"],
                ch.get("position", 0), published_at, updated_at,
                ch.get("content_hash"), ch.get("word_count"), ts, ts,
            ),
        )

        if prior is None:
            return "new"

        # A NULL content_hash means no chapter page has ever been read for this
        # row -- it was seeded from the ToC. Fill it in without logging history.
        if prior["content_hash"] is None:
            return "baseline"

        content_moved = (
            updated_at != prior["updated_at"]
            or ch.get("content_hash") != prior["content_hash"]
        )
        if not content_moved:
            return "retitled" if ch["title"] != prior["title"] else "unchanged"

        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE fiction_id = ? AND rr_chapter_id = ?",
            (fiction_id, ch["chapter_id"]),
        ).fetchone()["id"]

        old_wc, new_wc = prior["word_count"], ch.get("word_count")
        conn.execute(
            """
            INSERT INTO revisions (
                chapter_id, observed_at, old_updated_at, new_updated_at,
                old_hash, new_hash, word_delta
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chapter_id, ts, prior["updated_at"], updated_at,
                prior["content_hash"], ch.get("content_hash"),
                (new_wc - old_wc) if (old_wc is not None and new_wc is not None) else None,
            ),
        )
        return "edited"


def mark_refreshed(conn, fiction_id):
    with conn:
        conn.execute(
            "UPDATE fictions SET last_refreshed_at = ? WHERE id = ?",
            (now_iso(), fiction_id),
        )


def mark_read(conn, fiction_id, up_to_position=None, at=None):
    """
    Mark chapters read up to a position (or all of them).

    `at` overrides the timestamp. Defaulting to now() is right for real use, but a
    function that can only ever write "now" is untestable without sleeping, and it
    also blocks the honest case of recording that you read something yesterday.
    """
    sql = "UPDATE chapters SET last_read_at = ? WHERE fiction_id = ?"
    params = [to_iso(at) or now_iso(), fiction_id]
    if up_to_position is not None:
        sql += " AND position <= ?"
        params.append(up_to_position)
    with conn:
        conn.execute(sql, params)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_chapters(conn, fiction_id, order="position"):
    orders = {
        "position": "position ASC",
        "updated": f"{EFFECTIVE_UPDATED} DESC",
        "published": "published_at DESC",
    }
    return conn.execute(
        f"SELECT * FROM chapters WHERE fiction_id = ? ORDER BY {orders[order]}",
        (fiction_id,),
    ).fetchall()


def needs_reread(conn, fiction_id):
    """
    Chapters edited since you last read them -- the question the tool exists for.

    Note `last_read_at IS NOT NULL AND updated_at > last_read_at` rather than a
    bare comparison. In SQL, any comparison involving NULL yields NULL, which is
    not true, so those rows drop out of a WHERE clause silently. Being explicit
    about which NULLs are intended keeps that from looking like a bug later.
    """
    return conn.execute(
        """
        SELECT * FROM chapters
        WHERE fiction_id = ?
          AND updated_at IS NOT NULL
          AND last_read_at IS NOT NULL
          AND updated_at > last_read_at
        ORDER BY position ASC
        """,
        (fiction_id,),
    ).fetchall()


def stale_first(conn, fiction_id, limit=None):
    """
    Refresh ordering for partial sweeps. Conditional requests are unavailable on
    RoyalRoad (no ETag/Last-Modified, Cache-Control: no-store), so every check
    costs a full page fetch and a full sweep of a 300-chapter fiction runs ~9
    minutes. Checking recently-touched chapters first is the cheap approximation:
    observed edits cluster shortly after publication.
    """
    sql = f"""
        SELECT * FROM chapters
        WHERE fiction_id = ?
        ORDER BY {EFFECTIVE_UPDATED} DESC
    """
    params = [fiction_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def revision_history(conn, fiction_id):
    """The changelog: every observed edit, newest first."""
    return conn.execute(
        """
        SELECT r.*, c.title, c.position
        FROM revisions r
        JOIN chapters c ON c.id = r.chapter_id
        WHERE c.fiction_id = ?
        ORDER BY r.observed_at DESC, c.position ASC
        """,
        (fiction_id,),
    ).fetchall()


def never_edited(conn, fiction_id):
    """Uses the meaningful NULL directly -- IS NULL, not a comparison."""
    return conn.execute(
        "SELECT * FROM chapters WHERE fiction_id = ? AND updated_at IS NULL"
        " ORDER BY position ASC",
        (fiction_id,),
    ).fetchall()
