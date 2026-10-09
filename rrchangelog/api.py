"""FastAPI app: library CRUD, sync jobs, and the static frontend.

Run with:  .venv/bin/python -m rrchangelog     (or: uvicorn rrchangelog.api:app)

Settings (environment variables):
    RR_DB_PATH        SQLite file (default: data/rr-changelog.sqlite3)
    RR_FETCHER        auto | http | browser (default: auto)
    RR_DELAY          seconds between RoyalRoad requests (default: 1.0)
    RR_SHOW_BROWSER   1 to show the Playwright window, e.g. to solve a Cloudflare challenge
    RR_LOG_LEVEL      e.g. DEBUG to log every fetch's duration (default: INFO; python -m rrchangelog only)
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .scraper import BASE_URL, PageSource, parse_fiction_id
from .worker import SyncWorker

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


def default_source_factory() -> PageSource:
    return PageSource(
        fetcher=os.environ.get("RR_FETCHER", "auto"),
        delay=float(os.environ.get("RR_DELAY", "1.0")),
        show_browser=os.environ.get("RR_SHOW_BROWSER", "") not in ("", "0"),
    )


class AddFiction(BaseModel):
    url: str


def create_app(db_path: Path | str | None = None,
               source_factory: Callable[[], PageSource] = default_source_factory) -> FastAPI:
    db_path = db_path or os.environ.get("RR_DB_PATH", ROOT / "data" / "rr-changelog.sqlite3")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(db_path)
        db.init_db(conn)
        conn.close()
        app.state.worker = SyncWorker(db_path, source_factory)
        app.state.worker.start()
        yield
        app.state.worker.stop()

    app = FastAPI(title="RR Changelog", lifespan=lifespan)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def enqueue(request: Request, conn: sqlite3.Connection, fiction_id: int) -> dict:
        run_id = db.create_run(conn, fiction_id)
        request.app.state.worker.submit(run_id)
        return run_dict(conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone())

    def fiction_or_404(conn: sqlite3.Connection, fiction_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM fictions WHERE id = ?", (fiction_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Fiction not found")
        return row

    @app.get("/api/fictions")
    def list_fictions(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        rows = conn.execute("SELECT * FROM fictions ORDER BY title COLLATE NOCASE").fetchall()
        return [fiction_summary(conn, row) for row in rows]

    @app.post("/api/fictions", status_code=201)
    def add_fiction(body: AddFiction, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            rr_id = parse_fiction_id(body.url)
        except ValueError:
            raise HTTPException(400, "That doesn't look like a RoyalRoad fiction or chapter URL.")
        existing = conn.execute("SELECT id FROM fictions WHERE rr_fiction_id = ?", (rr_id,)).fetchone()
        if existing:
            raise HTTPException(409, {"message": "That fiction is already in your library.", "id": existing["id"]})
        # Title and author are filled in by the first sync.
        cur = conn.execute(
            "INSERT INTO fictions (rr_fiction_id, title, url, added_at) VALUES (?, ?, ?, ?)",
            (rr_id, f"Fiction {rr_id}", f"{BASE_URL}/fiction/{rr_id}", db.now()),
        )
        conn.commit()
        job = enqueue(request, conn, cur.lastrowid)
        return {"fiction": fiction_summary(conn, fiction_or_404(conn, cur.lastrowid)), "job": job}

    @app.get("/api/fictions/{fiction_id}")
    def get_fiction(fiction_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        fiction = fiction_or_404(conn, fiction_id)
        chapters = conn.execute("SELECT * FROM chapters WHERE fiction_id = ? ORDER BY position",
                                (fiction_id,)).fetchall()
        last_done = conn.execute("SELECT id FROM sync_runs WHERE fiction_id = ? AND status = 'done' "
                                 "ORDER BY id DESC LIMIT 1", (fiction_id,)).fetchone()
        recent: dict[int, list[str]] = {}
        if last_done:
            for c in conn.execute("SELECT chapter_id, change_type FROM chapter_changes WHERE sync_run_id = ?",
                                  (last_done["id"],)):
                recent.setdefault(c["chapter_id"], []).append(c["change_type"])
        return {
            **fiction_summary(conn, fiction),
            "chapters": [{**chapter_dict(ch), "recent_changes": recent.get(ch["id"], [])} for ch in chapters],
        }

    @app.delete("/api/fictions/{fiction_id}", status_code=204)
    def delete_fiction(fiction_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        fiction_or_404(conn, fiction_id)
        conn.execute("DELETE FROM fictions WHERE id = ?", (fiction_id,))
        conn.commit()
        return Response(status_code=204)

    @app.post("/api/fictions/{fiction_id}/refresh", status_code=202)
    def refresh_fiction(fiction_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        fiction_or_404(conn, fiction_id)
        if (running := db.active_run(conn, fiction_id)) is not None:
            raise HTTPException(409, {"message": "A sync is already queued or running.", "job": run_dict(running)})
        return enqueue(request, conn, fiction_id)

    @app.post("/api/refresh-all", status_code=202)
    def refresh_all(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        ids = [r["id"] for r in conn.execute("SELECT id FROM fictions ORDER BY title COLLATE NOCASE")]
        return [enqueue(request, conn, fid) for fid in ids if db.active_run(conn, fid) is None]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        return run_dict(row)

    @app.get("/api/fictions/{fiction_id}/changes")
    def get_changes(fiction_id: int, limit: int = 500, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        fiction_or_404(conn, fiction_id)
        rows = conn.execute(
            "SELECT cc.*, ch.title AS chapter_title, ch.url AS chapter_url, ch.position "
            "FROM chapter_changes cc JOIN chapters ch ON ch.id = cc.chapter_id "
            "WHERE ch.fiction_id = ? ORDER BY cc.id DESC LIMIT ?", (fiction_id, limit)).fetchall()
        return [dict(r) for r in rows]

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def run_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def chapter_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["last_updated_at"] = d["edited_at"] or d["published_at"]
    return d


def fiction_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    stats = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE status != 'removed') AS chapter_count, "
        "COUNT(*) FILTER (WHERE status != 'removed' AND edited_at IS NOT NULL) AS edited_count, "
        "MAX(COALESCE(edited_at, published_at)) FILTER (WHERE status != 'removed') AS latest_update_at "
        "FROM chapters WHERE fiction_id = ?", (row["id"],)).fetchone()
    last_run = conn.execute("SELECT * FROM sync_runs WHERE fiction_id = ? ORDER BY id DESC LIMIT 1",
                            (row["id"],)).fetchone()
    last_done = conn.execute("SELECT * FROM sync_runs WHERE fiction_id = ? AND status = 'done' "
                             "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    first_done = conn.execute("SELECT MIN(id) FROM sync_runs WHERE fiction_id = ? AND status = 'done'",
                              (row["id"],)).fetchone()[0]
    return {
        **dict(row),
        **dict(stats),
        "last_run": run_dict(last_run),
        "last_completed_run": run_dict(last_done),
        # The first completed sync is a baseline: it records chapters but logs no changes.
        "last_completed_run_is_baseline": last_done is not None and last_done["id"] == first_done,
        "active_run": run_dict(db.active_run(conn, row["id"])),
    }


app = create_app()
