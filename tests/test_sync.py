"""Tests for rrchangelog.sync: chapter-list diffing, edit detection, and failure handling."""

import unittest

from rrchangelog import db
from rrchangelog.sync import sync_fiction

from .fakes import FICTION_ID, CloudflareBlocked, FakeSource, FetchError, ts


def chapters_v1():
    return [
        {"id": 1, "title": "1. Good Morning Brother", "published": ts(2018, 10, 28), "edited": ts(2021, 12, 3)},
        {"id": 2, "title": "2. Life's Little Problems", "published": ts(2018, 10, 29)},
        {"id": 3, "title": "3. The Bitter Truth", "published": ts(2018, 10, 30)},
    ]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.fid = self.conn.execute(
            "INSERT INTO fictions (rr_fiction_id, title, url, added_at) VALUES (?, 'Fiction', 'u', ?)",
            (FICTION_ID, db.now())).lastrowid
        self.source = FakeSource()

    def sync(self) -> dict:
        run_id = db.create_run(self.conn, self.fid)
        sync_fiction(self.conn, run_id, self.source)
        return dict(self.conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone())

    def chapters(self) -> dict[int, dict]:
        return {r["rr_chapter_id"]: dict(r) for r in self.conn.execute("SELECT * FROM chapters")}

    def changes(self, run_id) -> list[tuple]:
        return sorted(
            (r["change_type"], r["rr_chapter_id"]) for r in self.conn.execute(
                "SELECT cc.change_type, ch.rr_chapter_id FROM chapter_changes cc "
                "JOIN chapters ch ON ch.id = cc.chapter_id WHERE cc.sync_run_id = ?", (run_id,)))

    def test_first_sync_is_a_silent_baseline(self):
        self.source.set_fiction(chapters_v1())
        run = self.sync()
        self.assertEqual((run["status"], run["chapters_total"], run["chapters_checked"]), ("done", 3, 3))
        self.assertEqual((run["new_count"], run["edited_count"], run["removed_count"]), (0, 0, 0))
        self.assertEqual(self.changes(run["id"]), [])

        chapters = self.chapters()
        self.assertEqual(chapters[1]["edited_at"], "2021-12-03T12:00:00+00:00")
        self.assertIsNone(chapters[2]["edited_at"])
        self.assertEqual(chapters[1]["published_at"], "2018-10-28T12:00:00+00:00")
        self.assertTrue(all(c["last_checked_at"] for c in chapters.values()))

        fiction = self.conn.execute("SELECT * FROM fictions").fetchone()
        self.assertEqual((fiction["title"], fiction["author"]), ("Mother of Learning", "nobody103"))
        self.assertEqual(fiction["cover_url"], "https://example.com/cover.jpg")
        self.assertIsNotNone(fiction["last_synced_at"])

    def test_second_sync_logs_new_removed_retitled_and_edited(self):
        self.source.set_fiction(chapters_v1())
        self.sync()

        v2 = chapters_v1()
        v2[0]["title"] = "1. Good Morning, Brother"   # retitled
        v2[0]["edited"] = ts(2023, 1, 1)               # edit date changed
        v2[2]["edited"] = ts(2023, 2, 2)               # first edit
        del v2[1]                                      # chapter 2 removed
        v2.append({"id": 4, "title": "4. Stumbling", "published": ts(2023, 3, 3), "edited": ts(2023, 3, 4)})
        self.source.set_fiction(v2)
        run = self.sync()

        self.assertEqual(self.changes(run["id"]),
                         [("edited", 1), ("edited", 3), ("new", 4), ("removed", 2), ("retitled", 1)])
        self.assertEqual((run["new_count"], run["edited_count"], run["removed_count"]), (1, 2, 1))
        chapters = self.chapters()
        self.assertEqual(chapters[2]["status"], "removed")
        self.assertEqual(chapters[4]["edited_at"], "2023-03-04T12:00:00+00:00")  # stored, not logged as an edit
        self.assertNotIn("chapter-2", self.source.requests[-3:])                  # removed chapters aren't fetched
        self.assertEqual(run["chapters_total"], 3)

    def test_unchanged_resync_logs_nothing(self):
        self.source.set_fiction(chapters_v1())
        self.sync()
        run = self.sync()
        self.assertEqual(self.changes(run["id"]), [])

    def test_removed_chapter_coming_back_is_restored(self):
        self.source.set_fiction(chapters_v1())
        self.sync()
        self.source.set_fiction(chapters_v1()[:2])
        self.sync()
        self.source.set_fiction(chapters_v1())
        run = self.sync()
        self.assertEqual(self.changes(run["id"]), [("restored", 3)])
        self.assertEqual(self.chapters()[3]["status"], "active")

    def test_locked_chapters_are_skipped_until_unlocked(self):
        v1 = chapters_v1()
        v1[2]["locked"] = True
        self.source.set_fiction(v1)
        run = self.sync()
        self.assertEqual(run["chapters_total"], 2)
        self.assertNotIn("chapter-3", self.source.requests)
        self.assertEqual(self.chapters()[3]["status"], "locked")

        self.source.set_fiction(chapters_v1())
        run = self.sync()
        self.assertEqual(self.changes(run["id"]), [("unlocked", 3)])

    def test_cloudflare_block_fails_the_run(self):
        self.source.pages = {"fiction": CloudflareBlocked("u")}
        run = self.sync()
        self.assertEqual(run["status"], "failed")
        self.assertIn("Cloudflare", run["error"])
        self.assertIsNone(self.conn.execute("SELECT last_synced_at FROM fictions").fetchone()[0])

    def test_one_failing_chapter_does_not_stop_the_sync(self):
        self.source.set_fiction(chapters_v1())
        self.source.pages["chapter-2"] = FetchError("HTTP 500 for u")
        run = self.sync()
        self.assertEqual((run["status"], run["error_count"], run["chapters_checked"]), ("done", 1, 3))
        self.assertEqual(self.chapters()[2]["check_error"], "HTTP 500 for u")

    def test_fiction_removed_while_queued(self):
        run_id = db.create_run(self.conn, self.fid)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DELETE FROM fictions")
        sync_fiction(self.conn, run_id, self.source)
        self.assertEqual(self.conn.execute("SELECT status FROM sync_runs").fetchone()[0], "failed")


if __name__ == "__main__":
    unittest.main()
