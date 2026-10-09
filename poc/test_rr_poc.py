"""Tests for rr_poc.py.

The HTML below is hand-written. CHAPTER_PAGE mirrors live legacy-layout markup;
the fiction pages mirror structures documented by FanFicFare's RoyalRoad adapter
(window.chapters JSON, redesign JSON-LD, legacy table#chapters). Full saved pages
are kept out of the repo since they contain the authors' chapter text.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import rr_poc

REDESIGN_FICTION = """<!DOCTYPE html><html><head><title>Test Story | Royal Road</title>
<script type="application/ld+json">{"@type":"Book","name":"Test Story",
 "author":{"@type":"Person","name":"Some Author","url":"https://www.royalroad.com/profile/42"},
 "datePublished":"2021-03-28T16:13:23Z","dateModified":"2026-10-01T12:00:00Z"}</script>
</head><body>
<script>
  window.fictionId = 41656;
  window.chapters = [{"id":654078,"volumeId":null,"title":"CH1 Prologue","slug":"ch1-prologue","date":"2021-03-28T16:13:23Z","order":0,"visible":1,"subscriptionTiers":null,"doesNotRollOver":false,"isUnlocked":true,"url":"/fiction/41656/test-story/chapter/654078/ch1-prologue"},{"id":654100,"volumeId":null,"title":"CH2 [x];","slug":"ch2","date":"2021-04-01T10:00:00Z","order":1,"visible":1,"subscriptionTiers":null,"doesNotRollOver":false,"isUnlocked":true,"url":"/fiction/41656/test-story/chapter/654100/ch2"},{"id":654200,"volumeId":null,"title":"CH3 Advance","slug":"ch3","date":"2026-10-01T12:00:00Z","order":2,"visible":1,"subscriptionTiers":[1],"doesNotRollOver":false,"isUnlocked":false,"url":"/fiction/41656/test-story/chapter/654200/ch3"}];
  window.volumes = [];
</script></body></html>"""

LEGACY_FICTION = """<html><head><title>Old Story | Royal Road</title>
<meta property="og:title" content="Old Story"><meta property="books:author" content="Legacy Author">
</head><body><table id="chapters" class="table"><thead><tr><th>Name</th><th>Release Date</th></tr></thead>
<tbody>
<tr class="chapter-row"><td><a href="/fiction/100/old-story/chapter/1001/one">Chapter One</a></td>
    <td><a href="/fiction/100/old-story/chapter/1001/one"><time unixtime="1600000000" title="Sunday, September 13, 2020">4 years ago</time></a></td></tr>
<tr class="chapter-row"><td><a href="/fiction/100/old-story/chapter/1002/two">Chapter Two</a></td>
    <td><a href="/fiction/100/old-story/chapter/1002/two"><time unixtime="1600100000">4 years ago</time></a></td></tr>
</tbody></table></body></html>"""

# Mirrors live legacy-layout chapter markup captured 2026-10-09 (chapter text omitted).
CHAPTER_PAGE = """<html><head><title>CH1 Prologue - Test Story | Royal Road</title></head><body>
<div class="fic-header"><h1>CH1 Prologue</h1>
  <ul class="list-inline">
    <li>
        <i class="fa fa-calendar" title="Published"></i> <time unixtime="1616948003" datetime="2021-03-28T16:13:23.0000000Z" format="U" >Sunday, March 28, 2021 4:13:23 PM</time>
    </li>
        <li>
            <i class="fa fa-edit" title="Last Edit"></i> <time unixtime="1641371400" datetime="2022-01-05T08:30:00.0000000Z" format="U" >Wednesday, January 5, 2022 8:30:00 AM</time>
        </li>
  </ul>
</div>
<div class="chapter-inner chapter-content"><p>It was a dark and stormy night, published in 2021.</p></div>
</body></html>"""

CHALLENGE_PAGE = """<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><script>window._cf_chl_opt={cvId:'3'};</script></body></html>"""


class ParseFictionIdTests(unittest.TestCase):
    def test_accepts_fiction_chapter_and_bare_ids(self):
        self.assertEqual(rr_poc.parse_fiction_id("https://www.royalroad.com/fiction/21220/mother-of-learning"), 21220)
        self.assertEqual(rr_poc.parse_fiction_id("royalroad.com/fiction/21220/x/chapter/301778/1-good-morning"), 21220)
        self.assertEqual(rr_poc.parse_fiction_id(" 21220 "), 21220)

    def test_rejects_other_urls(self):
        with self.assertRaises(ValueError):
            rr_poc.parse_fiction_id("https://example.com/fiction/1")


class ParseFictionTests(unittest.TestCase):
    def test_redesign_window_chapters_and_json_ld(self):
        f = rr_poc.parse_fiction(REDESIGN_FICTION, 41656)
        self.assertEqual((f.title, f.author), ("Test Story", "Some Author"))
        self.assertEqual(f.date_modified, datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
        self.assertEqual([c.id for c in f.chapters], [654078, 654100, 654200])
        self.assertEqual(f.chapters[1].title, "CH2 [x];")  # brackets/semicolons in titles don't break JSON extraction
        self.assertEqual(f.chapters[0].url, "https://www.royalroad.com/fiction/41656/test-story/chapter/654078/ch1-prologue")
        self.assertEqual(f.chapters[0].published, datetime(2021, 3, 28, 16, 13, 23, tzinfo=timezone.utc))
        self.assertEqual([c.locked for c in f.chapters], [False, False, True])

    def test_legacy_table_fallback(self):
        f = rr_poc.parse_fiction(LEGACY_FICTION, 100)
        self.assertEqual((f.title, f.author), ("Old Story", "Legacy Author"))
        self.assertEqual([(c.id, c.title) for c in f.chapters], [(1001, "Chapter One"), (1002, "Chapter Two")])
        self.assertEqual(f.chapters[0].published, datetime.fromtimestamp(1600000000, tz=timezone.utc))

    def test_no_chapters_raises(self):
        with self.assertRaises(ValueError):
            rr_poc.parse_fiction("<html><body>nothing</body></html>", 1)


class ParseChapterDatesTests(unittest.TestCase):
    def test_classifies_labelled_time_elements(self):
        d = rr_poc.parse_chapter_dates(CHAPTER_PAGE)
        self.assertEqual(d.published.value, datetime(2021, 3, 28, 16, 13, 23, tzinfo=timezone.utc))
        self.assertEqual(d.updated.value, datetime(2022, 1, 5, 8, 30, tzinfo=timezone.utc))
        self.assertIn("Last Edit", d.updated.source)

    def test_meta_and_json_ld_candidates(self):
        page = """<html><head><meta property="article:modified_time" content="2023-02-02T00:00:00Z">
        <script type="application/ld+json">{"@type":"Article","datePublished":"2023-01-01T00:00:00Z"}</script>
        </head><body></body></html>"""
        d = rr_poc.parse_chapter_dates(page)
        self.assertEqual(d.updated.source, "meta article:modified_time")
        self.assertEqual(d.published.source, "json-ld Article.datePublished")

    def test_no_dates(self):
        d = rr_poc.parse_chapter_dates("<html><body><p>hi</p></body></html>")
        self.assertIsNone(d.updated)
        self.assertEqual(d.candidates, [])


    def test_unedited_chapter_has_no_updated_date(self):
        page = CHAPTER_PAGE.split("<li>\n            <i class=\"fa fa-edit\"")[0] + "</ul></div></body></html>"
        d = rr_poc.parse_chapter_dates(page)
        self.assertIsNotNone(d.published)
        self.assertIsNone(d.updated)


class CloudflareTests(unittest.TestCase):
    def test_detects_interstitial_only(self):
        self.assertTrue(rr_poc.is_cloudflare_challenge(CHALLENGE_PAGE))
        normal = REDESIGN_FICTION.replace("</body>", '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body>')
        self.assertFalse(rr_poc.is_cloudflare_challenge(normal))


class EndToEndOfflineTests(unittest.TestCase):
    def test_from_dir_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "fiction.html").write_text(REDESIGN_FICTION)
            Path(tmp, "chapter-654078.html").write_text(CHAPTER_PAGE)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = rr_poc.main(["https://www.royalroad.com/fiction/41656/test-story", "--from-dir", tmp, "--json"])
        self.assertEqual(code, 0)
        chapters = json.loads(out.getvalue())["chapters"]
        self.assertTrue(chapters[0]["last_updated"].startswith("2022-01-05 08:30"))
        self.assertEqual(chapters[1]["note"], "not in --from-dir")
        self.assertEqual(chapters[2]["note"], "locked (premium/advance)")

    def test_table_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "fiction.html").write_text(REDESIGN_FICTION)
            Path(tmp, "chapter-654078.html").write_text(CHAPTER_PAGE)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rr_poc.main(["41656", "--from-dir", tmp])
        self.assertIn("2022-01-05 08:30", out.getvalue())
        self.assertIn("1/3 chapters had an 'updated' date; 1 differ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
