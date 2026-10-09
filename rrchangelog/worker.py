"""Background sync worker: a single thread that runs queued sync runs one at a time.

One thread means one PageSource, so every sync shares the same request throttle and
(if Cloudflare forces it) the same browser. Playwright objects are thread-bound,
which is another reason the source lives on this thread only.
"""

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from . import db
from .scraper import PageSource
from .sync import sync_fiction

log = logging.getLogger(__name__)

_STOP = object()


class SyncWorker:
    def __init__(self, db_path: Path | str, source_factory: Callable[[], PageSource]):
        self.db_path = db_path
        self.source_factory = source_factory
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="sync-worker", daemon=True)

    def start(self) -> None:
        conn = db.connect(self.db_path)
        try:
            # A restart kills whatever was running; queued runs can simply be picked up again.
            conn.execute("UPDATE sync_runs SET status = 'interrupted', finished_at = ?, "
                         "error = 'Server restarted during sync' WHERE status = 'running'", (db.now(),))
            conn.commit()
            pending = [r["id"] for r in conn.execute("SELECT id FROM sync_runs WHERE status = 'queued' ORDER BY id")]
        finally:
            conn.close()
        self._thread.start()
        for run_id in pending:
            self.submit(run_id)

    def submit(self, run_id: int) -> None:
        self._queue.put(run_id)

    def stop(self, timeout: float = 10) -> None:
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        conn = db.connect(self.db_path)
        source: PageSource | None = None
        try:
            while (run_id := self._queue.get()) is not _STOP:
                if source is None:
                    source = self.source_factory()
                log.info("sync run %s starting", run_id)
                sync_fiction(conn, run_id, source)
                log.info("sync run %s finished", run_id)
        finally:
            if source is not None:
                source.close()
            conn.close()
