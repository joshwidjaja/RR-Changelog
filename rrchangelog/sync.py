"""One sync run: diff a fiction's chapter list against the DB, then re-check every chapter page.

The first successful sync of a fiction is a baseline: chapters are stored but no
"new"/"edited" changes are logged, so the changelog only shows what changed afterwards.
"""

import logging
import sqlite3

from . import db
from .scraper import BASE_URL, CloudflareBlocked, FetchError, PageSource, parse_chapter_dates, parse_fiction

log = logging.getLogger(__name__)


def sync_fiction(conn: sqlite3.Connection, run_id: int, source: PageSource) -> None:
    run = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    fiction = conn.execute("SELECT * FROM fictions WHERE id = ?", (run["fiction_id"],)).fetchone()
    if fiction is None:  # removed from the library while queued
        _finish(conn, run_id, "failed", error="fiction was removed")
        return

    conn.execute("UPDATE sync_runs SET status = 'running', started_at = ? WHERE id = ?", (db.now(), run_id))
    conn.commit()
    try:
        _sync(conn, run_id, fiction, source)
    except CloudflareBlocked:
        _finish(conn, run_id, "failed", error="Blocked by Cloudflare. Try again later, or run with "
                                              "RR_SHOW_BROWSER=1 and complete the challenge in the window.")
    except (FetchError, ValueError) as e:
        _finish(conn, run_id, "failed", error=str(e))
    except Exception as e:
        log.exception("sync run %s crashed", run_id)
        _finish(conn, run_id, "failed", error=f"Unexpected error: {e}")


def _sync(conn: sqlite3.Connection, run_id: int, fiction: sqlite3.Row, source: PageSource) -> None:
    fid, rr_id = fiction["id"], fiction["rr_fiction_id"]
    baseline = fiction["last_synced_at"] is None
    parsed = parse_fiction(source.get("fiction", f"{BASE_URL}/fiction/{rr_id}"), rr_id)

    conn.execute(
        "UPDATE fictions SET title = ?, author = ?, cover_url = ? WHERE id = ?",
        (parsed.title, parsed.author, parsed.cover_url or fiction["cover_url"], fid),
    )
    counts = _diff_chapter_list(conn, run_id, fid, parsed.chapters, baseline)
    conn.commit()

    to_check = conn.execute(
        "SELECT * FROM chapters WHERE fiction_id = ? AND status = 'active' ORDER BY position", (fid,)
    ).fetchall()
    conn.execute("UPDATE sync_runs SET chapters_total = ? WHERE id = ?", (len(to_check), run_id))
    conn.commit()

    for checked, chapter in enumerate(to_check, 1):
        try:
            body = source.get(f"chapter-{chapter['rr_chapter_id']}", chapter["url"])
        except FetchError as e:
            counts["error"] += 1
            conn.execute("UPDATE chapters SET check_error = ?, last_checked_at = ? WHERE id = ?",
                         (str(e), db.now(), chapter["id"]))
        else:
            if body is not None:
                _record_chapter_dates(conn, run_id, chapter, body, baseline, counts)
        conn.execute("UPDATE sync_runs SET chapters_checked = ?, edited_count = ?, error_count = ? WHERE id = ?",
                     (checked, counts["edited"], counts["error"], run_id))
        conn.commit()  # per chapter, so the UI sees progress

    conn.execute("UPDATE fictions SET last_synced_at = ? WHERE id = ?", (db.now(), fid))
    _finish(conn, run_id, "done", counts=counts)


def _diff_chapter_list(conn, run_id, fid, chapters, baseline) -> dict:
    counts = {"new": 0, "edited": 0, "removed": 0, "error": 0}
    existing = {row["rr_chapter_id"]: row for row in conn.execute(
        "SELECT * FROM chapters WHERE fiction_id = ?", (fid,))}
    seen = set()

    for position, ch in enumerate(chapters):
        seen.add(ch.id)
        status = "locked" if ch.locked else "active"
        published = db.iso(ch.published)
        row = existing.get(ch.id)
        if row is None:
            cur = conn.execute(
                "INSERT INTO chapters (fiction_id, rr_chapter_id, title, url, position, published_at, status, "
                "first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, ch.id, ch.title, ch.url, position, published, status, db.now()),
            )
            if not baseline:
                _change(conn, run_id, cur.lastrowid, "new", None, ch.title)
                counts["new"] += 1
            continue

        if row["title"] != ch.title:
            _change(conn, run_id, row["id"], "retitled", row["title"], ch.title)
        if row["status"] == "removed":
            _change(conn, run_id, row["id"], "restored", None, None)
        elif row["status"] == "locked" and status == "active":
            _change(conn, run_id, row["id"], "unlocked", None, None)
        conn.execute(
            "UPDATE chapters SET title = ?, url = ?, position = ?, published_at = COALESCE(?, published_at), "
            "status = ? WHERE id = ?",
            (ch.title, ch.url, position, published, status, row["id"]),
        )

    for rr_chapter_id, row in existing.items():
        if rr_chapter_id not in seen and row["status"] != "removed":
            conn.execute("UPDATE chapters SET status = 'removed' WHERE id = ?", (row["id"],))
            _change(conn, run_id, row["id"], "removed", row["title"], None)
            counts["removed"] += 1
    return counts


def _record_chapter_dates(conn, run_id, chapter, body, baseline, counts) -> None:
    dates = parse_chapter_dates(body)
    edited = db.iso(dates.updated.value) if dates.updated else None
    page_published = db.iso(dates.published.value) if dates.published else None
    # A chapter is only "edited" relative to an earlier check; new chapters just get their dates.
    if edited != chapter["edited_at"] and chapter["last_checked_at"] is not None and not baseline:
        _change(conn, run_id, chapter["id"], "edited", chapter["edited_at"], edited)
        counts["edited"] += 1
    conn.execute(
        "UPDATE chapters SET edited_at = ?, published_at = COALESCE(published_at, ?), last_checked_at = ?, "
        "check_error = NULL WHERE id = ?",
        (edited, page_published, db.now(), chapter["id"]),
    )


def _change(conn, run_id, chapter_id, change_type, old, new) -> None:
    conn.execute(
        "INSERT INTO chapter_changes (chapter_id, sync_run_id, change_type, old_value, new_value, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chapter_id, run_id, change_type, old, new, db.now()),
    )


def _finish(conn, run_id, status, *, counts=None, error=None) -> None:
    counts = counts or {}
    conn.execute(
        "UPDATE sync_runs SET status = ?, finished_at = ?, error = ?, "
        "new_count = COALESCE(?, new_count), edited_count = COALESCE(?, edited_count), "
        "removed_count = COALESCE(?, removed_count), error_count = COALESCE(?, error_count) WHERE id = ?",
        (status, db.now(), error, counts.get("new"), counts.get("edited"), counts.get("removed"),
         counts.get("error"), run_id),
    )
    conn.commit()
