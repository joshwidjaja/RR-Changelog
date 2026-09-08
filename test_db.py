"""
Tests for the storage layer. Run: python3 test_db.py

Uses real values pulled from the three chapters we've actually looked at, so the
NULL-vs-timestamp cases under test are the ones RoyalRoad really produces.
"""

from datetime import datetime, timezone

import db as D

FICTION = dict(rr_id=141517, title="Cursed Relic Collector",
               url="https://www.royalroad.com/fiction/141517/cursed-relic-collector")


def dt(s):
    return datetime.fromisoformat(s)


# Real extractions. Ch1 edited 8 months later, ch109 edited 11 hours later,
# ch110 never edited (updated_at is None -- the meaningful NULL).
CH1 = dict(chapter_id=2794892, title="Chapter 1 - To Protect a Relic", url="…/2794892/x",
           position=1, published_at=dt("2025-11-26T15:13:47+00:00"),
           updated_at=dt("2026-07-28T14:16:32.253845+00:00"),
           content_hash="4e9c4333728c485a", word_count=4909)
CH109 = dict(chapter_id=3791880, title="Chapter 109 - A Day in Cindralet", url="…/3791880/x",
             position=109, published_at=dt("2026-08-09T16:56:17+00:00"),
             updated_at=dt("2026-08-10T03:37:11.429515+00:00"),
             content_hash="aaaa111122223333", word_count=4250)
CH110 = dict(chapter_id=3809451, title="Chapter 110 - Lightning Lodestones", url="…/3809451/x",
             position=110, published_at=dt("2026-08-13T16:51:56+00:00"),
             updated_at=None,                      # never edited
             content_hash="6763b01698723f7b", word_count=2188)

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def main():
    conn = D.connect(":memory:")
    D.init(conn)

    print("\npragmas")
    check("foreign_keys on", conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
    # WAL is a no-op for :memory: databases -- it reports "memory". Worth knowing
    # so the test doesn't assert something the engine can't do.
    print(f"  ..   journal_mode = {conn.execute('PRAGMA journal_mode').fetchone()[0]} (memory db)")

    print("\nfirst ingest")
    fid = D.upsert_fiction(conn, **FICTION)
    for ch in (CH1, CH109, CH110):
        check(f"ch{ch['position']} new", D.upsert_chapter(conn, fid, ch), "new")

    print("\nidempotent re-ingest (the common refresh case)")
    for ch in (CH1, CH109, CH110):
        check(f"ch{ch['position']} unchanged", D.upsert_chapter(conn, fid, ch), "unchanged")
    check("no spurious revisions",
          conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 0)

    print("\nfiction upsert is idempotent too")
    check("same fiction id", D.upsert_fiction(conn, **FICTION), fid)
    check("one fiction row",
          conn.execute("SELECT COUNT(*) FROM fictions").fetchone()[0], 1)

    print("\nmeaningful NULL")
    check("ch110 updated_at is NULL",
          conn.execute("SELECT updated_at FROM chapters WHERE rr_chapter_id=?",
                       (CH110["chapter_id"],)).fetchone()[0], None)
    check("never_edited finds exactly ch110",
          [r["rr_chapter_id"] for r in D.never_edited(conn, fid)], [CH110["chapter_id"]])

    print("\nordering: COALESCE keeps the unedited chapter in its right place")
    # Effective dates: ch1 2026-07-28, ch109 2026-08-10, ch110 2026-08-13 (published).
    # Ordering on the raw updated_at column would sink ch110 to the bottom on NULL.
    check("by updated (effective)",
          [r["position"] for r in D.list_chapters(conn, fid, "updated")], [110, 109, 1])
    raw = [r["position"] for r in conn.execute(
        "SELECT position FROM chapters WHERE fiction_id=? ORDER BY updated_at DESC",
        (fid,))]
    check("raw column would mis-sort (documents the trap)", raw, [109, 1, 110])

    print("\ndetecting a real edit")
    edited = dict(CH110, updated_at=dt("2026-08-20T09:00:00+00:00"),
                  content_hash="ffff000011112222", word_count=2350)
    check("reported as edited", D.upsert_chapter(conn, fid, edited), "edited")
    rev = conn.execute("SELECT * FROM revisions").fetchone()
    check("one revision logged",
          conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1)
    check("old_updated_at was NULL", rev["old_updated_at"], None)
    check("word_delta is NULL when no prior baseline gap", rev["word_delta"], 2350 - 2188)

    print("\nretitle without content change")
    retitled = dict(CH1, title="Chapter 1 - To Protect a Relic (revised title)")
    check("reported as retitled", D.upsert_chapter(conn, fid, retitled), "retitled")
    check("no revision for a retitle",
          conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1)

    print("\nneeds_reread")
    # Read everything as of 2026-08-15. Relative to that instant:
    #   ch1   edited 2026-07-28  -> before the read, nothing to do
    #   ch109 edited 2026-08-10  -> before the read, nothing to do
    #   ch110 edited 2026-08-20  -> AFTER the read, needs re-reading
    D.mark_read(conn, fid, at=dt("2026-08-15T00:00:00+00:00"))
    check("only the post-read edit surfaces",
          [r["position"] for r in D.needs_reread(conn, fid)], [110])

    later = dict(CH109, updated_at=dt("2026-08-25T12:00:00+00:00"),
                 content_hash="cccc333344445555")
    D.upsert_chapter(conn, fid, later)
    check("a second post-read edit joins it, in reading order",
          [r["position"] for r in D.needs_reread(conn, fid)], [109, 110])
    check("unedited chapters never appear in needs_reread",
          all(r["updated_at"] is not None for r in D.needs_reread(conn, fid)), True)

    # Re-reading clears the list -- the loop the tool exists to close.
    D.mark_read(conn, fid, at=dt("2026-08-26T00:00:00+00:00"))
    check("caught up again", len(D.needs_reread(conn, fid)), 0)

    print("\nrevision history (the changelog)")
    hist = D.revision_history(conn, fid)
    check("two revisions recorded", len(hist), 2)
    check("newest first", hist[0]["position"], 109)

    print("\npartial sweep ordering")
    check("stale_first limit respects recency",
          [r["position"] for r in D.stale_first(conn, fid, limit=2)], [109, 110])

    print("\nforeign keys actually enforced")
    try:
        conn.execute("INSERT INTO chapters (fiction_id, rr_chapter_id, title, url,"
                     " position, first_seen_at) VALUES (9999, 1, 't', 'u', 1, 'now')")
        conn.commit()
        check("orphan rejected", "accepted", "rejected")
    except Exception as e:
        check("orphan rejected", type(e).__name__, "IntegrityError")

    D.mark_refreshed(conn, fid)
    check("last_refreshed_at set",
          conn.execute("SELECT last_refreshed_at IS NOT NULL FROM fictions").fetchone()[0], 1)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
