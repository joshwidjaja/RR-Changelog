"""
End-to-end test: add -> refresh -> detect an edit -> unread -> history.

Runs entirely offline against a fixture-backed fetcher, so it exercises the real
CLI code paths without touching RoyalRoad. Run: python3 test_e2e.py
"""

import argparse
import pathlib
import re
import tempfile

import cli
import db as D
import rr

FIX = pathlib.Path(__file__).parent / "fixtures"
passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


class FixtureFetcher:
    """
    Serves saved pages instead of the network.

    The two chapters we actually captured come back verbatim. The other 115 are
    synthesised, because the point of this test is the refresh loop's plumbing --
    change detection, revision logging, resumability -- not the parser, which
    test_rr.py already covers against real markup.
    """

    def __init__(self):
        self.edited = (FIX / "chapter_edited.html").read_text(encoding="utf-8")
        self.unedited = (FIX / "chapter_unedited.html").read_text(encoding="utf-8")
        self.toc = (FIX / "fiction.html").read_text(encoding="utf-8")
        self.calls = []
        self.bump = {}          # chapter_id -> fake "Last Edit" to simulate a later edit

    def get(self, url):
        self.calls.append(url)
        if "/chapter/" not in url:
            return self.toc
        cid = int(rr.CHAPTER_URL_RE.search(url).group(2))
        if cid == 2794892:
            return self.edited
        if cid == 3809451:
            return self.unedited
        return self._synthesise(cid)

    def _synthesise(self, cid, published="2026-01-01T00:00:00.0000000Z"):
        edit = self.bump.get(cid)
        edit_li = ""
        if edit:
            edit_li = (f'<li><i class="fa fa-edit" title="Last Edit"></i>'
                       f'<time datetime="{edit}">x</time></li>')
        body = f"synthetic body for {cid}" + (" plus revisions" if edit else "")
        return f"""<html><head>
          <meta property="og:url" content="https://www.royalroad.com/fiction/141517/f/chapter/{cid}/s">
          </head><body><h1>Chapter {cid}</h1>
          <ul class="list-inline">
            <li><i class="fa fa-calendar" title="Published"></i>
                <time datetime="{published}">x</time></li>
            {edit_li}
          </ul>
          <div class="chapter-inner chapter-content"><p>{body}</p></div>
        </body></html>"""

    def close(self):
        pass


def ns(**kw):
    base = dict(delay=0.0, verbose=False, recent=None, full=False,
                sort="position", limit=None, through=None)
    base.update(kw)
    return argparse.Namespace(**base)


def main():
    fetcher = FixtureFetcher()
    with tempfile.TemporaryDirectory() as tmp:
        conn = D.connect(f"{tmp}/test.db")
        D.init(conn)

        print("\nadd (seeds from the ToC)")
        toc = rr.parse_toc(fetcher.toc,
                           url="https://www.royalroad.com/fiction/141517/cursed-relic-collector")
        fid = D.upsert_fiction(conn, **toc["fiction"])
        for ch in toc["chapters"]:
            D.upsert_chapter(conn, fid, ch)
        check("117 chapters seeded",
              conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0], 117)
        check("no edit data from ToC alone",
              conn.execute("SELECT COUNT(*) FROM chapters WHERE updated_at IS NOT NULL")
              .fetchone()[0], 0)

        print("\nfirst full refresh (reads every chapter page)")
        cli.cmd_refresh(conn, ns(fiction="141517", full=True), fetcher=fetcher)
        check("117 chapter pages fetched",
              len([u for u in fetcher.calls if "/chapter/" in u]), 117)
        check("ch1 now shows its edit",
              conn.execute("SELECT updated_at FROM chapters WHERE rr_chapter_id=2794892")
              .fetchone()[0][:10], "2026-07-28")
        check("ch110 correctly stays NULL",
              conn.execute("SELECT updated_at FROM chapters WHERE rr_chapter_id=3809451")
              .fetchone()[0], None)
        # First sight of a chapter's edit date is not a *revision* -- there was
        # no prior observation to differ from. Revisions start on the 2nd pass.
        check("no revisions on first pass",
              conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 0)

        print("\nsecond refresh, nothing changed upstream")
        fetcher.calls.clear()
        changed = cli.cmd_refresh(conn, ns(fiction="141517", full=True), fetcher=fetcher)
        check("no changes reported", len(changed), 0)
        check("still no revisions",
              conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 0)

        print("\nauthor edits two chapters; refresh detects them")
        target = conn.execute(
            "SELECT rr_chapter_id FROM chapters WHERE rr_chapter_id NOT IN (2794892,3809451)"
            " ORDER BY position LIMIT 2").fetchall()
        for row in target:
            fetcher.bump[row[0]] = "2026-09-07T08:00:00.0000000Z"
        changed = cli.cmd_refresh(conn, ns(fiction="141517", full=True), fetcher=fetcher)
        check("both edits detected", len(changed), 2)
        check("two revisions logged",
              conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 2)
        rev = conn.execute("SELECT * FROM revisions ORDER BY id LIMIT 1").fetchone()
        check("revision records the word delta", rev["word_delta"] is not None, True)
        check("first edit recorded as from-NULL", rev["old_updated_at"], None)

        print("\npartial sweep only touches the recent tail")
        fetcher.calls.clear()
        cli.cmd_refresh(conn, ns(fiction="141517", recent=10), fetcher=fetcher)
        check("10 pages fetched, not 117",
              len([u for u in fetcher.calls if "/chapter/" in u]), 10)

        print("\nunread workflow")
        D.mark_read(conn, fid, at="2026-09-01T00:00:00+00:00")
        need = D.needs_reread(conn, fid)
        check("the two post-read edits surface", len(need), 2)
        D.mark_read(conn, fid, at="2026-09-08T00:00:00+00:00")
        check("caught up", len(D.needs_reread(conn, fid)), 0)

        print("\nhistory renders")
        hist = D.revision_history(conn, fid)
        check("history has both revisions", len(hist), 2)

        print("\nresolve accepts id or url")
        check("by url", cli.resolve(conn, toc["fiction"]["url"])["rr_id"], 141517)
        check("by id", cli.resolve(conn, "141517")["rr_id"], 141517)

        conn.close()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
