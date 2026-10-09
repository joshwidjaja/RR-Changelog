"""Tests for the FastAPI app, with the real worker thread and a fake page source."""

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from rrchangelog.api import create_app

from .fakes import FICTION_ID, FakeSource, ts

MOL_URL = f"https://www.royalroad.com/fiction/{FICTION_ID}/mother-of-learning"


def chapters():
    return [
        {"id": 301778, "title": "1. Good Morning Brother", "published": ts(2018, 10, 28), "edited": ts(2021, 12, 3)},
        {"id": 301781, "title": "2. Life's Little Problems", "published": ts(2018, 10, 28)},
    ]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = FakeSource()
        self.source.set_fiction(chapters())
        app = create_app(Path(self.tmp.name) / "test.sqlite3", source_factory=lambda: self.source)
        self.client = TestClient(app)
        self.client.__enter__()  # runs the lifespan: creates the DB and starts the worker

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def wait_for(self, job_id: int) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish")

    def add(self, url=MOL_URL) -> dict:
        res = self.client.post("/api/fictions", json={"url": url})
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertEqual(self.wait_for(body["job"]["id"])["status"], "done")
        return body

    def test_add_syncs_and_lists_fiction(self):
        body = self.add(f"{MOL_URL}/chapter/301778/1-good-morning-brother")  # chapter URLs work too
        fid = body["fiction"]["id"]

        library = self.client.get("/api/fictions").json()
        self.assertEqual(len(library), 1)
        f = library[0]
        self.assertEqual((f["title"], f["rr_fiction_id"], f["chapter_count"], f["edited_count"]),
                         ("Mother of Learning", FICTION_ID, 2, 1))
        self.assertTrue(f["last_completed_run_is_baseline"])
        self.assertIsNone(f["active_run"])
        self.assertEqual(f["latest_update_at"], "2021-12-03T12:00:00+00:00")

        detail = self.client.get(f"/api/fictions/{fid}").json()
        self.assertEqual([c["title"] for c in detail["chapters"]], ["1. Good Morning Brother", "2. Life's Little Problems"])
        self.assertEqual(detail["chapters"][1]["last_updated_at"], detail["chapters"][1]["published_at"])
        self.assertEqual(detail["chapters"][0]["recent_changes"], [])

    def test_rejects_bad_and_duplicate_urls(self):
        self.assertEqual(self.client.post("/api/fictions", json={"url": "https://example.com/x"}).status_code, 400)
        fid = self.add()["fiction"]["id"]
        dup = self.client.post("/api/fictions", json={"url": str(FICTION_ID)})
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(dup.json()["detail"]["id"], fid)

    def test_refresh_reports_changes(self):
        fid = self.add()["fiction"]["id"]
        updated = chapters()
        updated[1]["edited"] = ts(2024, 5, 5)
        self.source.set_fiction(updated)

        res = self.client.post(f"/api/fictions/{fid}/refresh")
        self.assertEqual(res.status_code, 202)
        job = self.wait_for(res.json()["id"])
        self.assertEqual(job["edited_count"], 1)

        detail = self.client.get(f"/api/fictions/{fid}").json()
        self.assertFalse(detail["last_completed_run_is_baseline"])
        self.assertEqual(detail["chapters"][1]["recent_changes"], ["edited"])
        changes = self.client.get(f"/api/fictions/{fid}/changes").json()
        self.assertEqual([(c["change_type"], c["new_value"]) for c in changes], [("edited", "2024-05-05T12:00:00+00:00")])

    def test_refresh_all_skips_fictions_already_syncing(self):
        self.add()
        jobs = self.client.post("/api/refresh-all").json()
        self.assertEqual(len(jobs), 1)
        again = self.client.post("/api/refresh-all").json()
        self.assertLessEqual(len(again), 1)  # 0 if the first refresh is still queued/running
        for job in jobs + again:
            self.wait_for(job["id"])

    def test_delete_removes_fiction_and_history(self):
        fid = self.add()["fiction"]["id"]
        self.assertEqual(self.client.delete(f"/api/fictions/{fid}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/fictions/{fid}").status_code, 404)
        self.assertEqual(self.client.get("/api/fictions").json(), [])

    def test_serves_frontend(self):
        self.assertIn("RR Changelog", self.client.get("/").text)
        self.assertEqual(self.client.get("/fiction.html").status_code, 200)

    def test_failed_sync_is_reported(self):
        from rrchangelog.scraper import CloudflareBlocked
        self.source.pages["fiction"] = CloudflareBlocked("u")
        res = self.client.post("/api/fictions", json={"url": MOL_URL})
        job = self.wait_for(res.json()["job"]["id"])
        self.assertEqual(job["status"], "failed")
        f = self.client.get("/api/fictions").json()[0]
        self.assertEqual(f["last_run"]["status"], "failed")
        self.assertIsNone(f["last_synced_at"])


if __name__ == "__main__":
    unittest.main()
