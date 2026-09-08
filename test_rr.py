"""
Adapter tests against saved fixtures. Run: python3 test_rr.py

Fixtures live in fixtures/ and are saved HTML, never the live site -- tests that
hit the network are slow, flaky, and rude. Both chapter markup shapes are
covered, which is the point: an edited chapter and an unedited one parse through
different branches.
"""

import pathlib
import re

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


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_toc():
    print("\ntable of contents")
    d = rr.parse_toc(load("fiction.html"),
                     url="https://www.royalroad.com/fiction/141517/cursed-relic-collector")
    f, ch = d["fiction"], d["chapters"]

    check("fiction id", f["rr_id"], 141517)
    check("fiction title", f["title"], "Cursed Relic Collector")
    check("author", f["author"], "Maradina")
    check("chapter count", len(ch), 117)

    first, last = ch[0], ch[-1]
    check("first chapter id", first["chapter_id"], 2794892)
    check("order normalised to 1-based", first["position"], 1)
    check("last position", last["position"], 117)
    check("published parsed", first["published_at"].isoformat(), "2025-11-26T15:13:47+00:00")
    check("no-fraction date parsed", last["published_at"].isoformat(), "2026-09-06T16:19:31+00:00")
    check("url absolutised", first["url"].startswith("https://www.royalroad.com/fiction/141517/"), True)

    check("positions contiguous",
          [c["position"] for c in ch] == list(range(1, 118)), True)
    check("all fetchable in this fixture", len(rr.fetchable(ch)), 117)

    # The ToC carries publication dates only. If it ever carried edit dates the
    # expensive per-chapter fetch would be unnecessary -- so assert the absence,
    # and this test starts failing the happy day that changes.
    check("ToC exposes no edit info",
          any("updated" in k or "edit" in k.lower() for k in ch[0]), False)


def test_toc_failure_modes():
    print("\ntoc failure modes")
    for label, html in [
        ("missing window.chapters", "<html><body>nothing here</body></html>"),
        ("malformed JSON", "<script>window.chapters = [{'bad': ;</script>"),
    ]:
        try:
            rr.parse_toc(html)
            check(label, "no error", "ParseError")
        except rr.ParseError:
            check(label, "ParseError", "ParseError")
        except Exception as e:                      # noqa: BLE001
            check(label, type(e).__name__, "ParseError")


def test_chapter_edited():
    print("\nchapter page: edited")
    c = rr.parse_chapter(load("chapter_edited.html"))
    check("chapter id", c["chapter_id"], 2794892)
    check("fiction id", c["fiction_id"], 141517)
    check("title", c["title"], "Chapter 1 - To Protect a Relic")
    check("published", c["published_at"].isoformat(), "2025-11-26T15:13:47+00:00")
    check("7-digit fraction trimmed to 6",
          c["updated_at"].isoformat(), "2026-07-28T14:16:32.253845+00:00")
    check("was_edited", c["was_edited"], True)
    check("word count excludes hidden injection", c["word_count"], 4892)
    check("hash", c["content_hash"], "6a7d98c250a675e6")


def test_chapter_unedited():
    print("\nchapter page: never edited")
    c = rr.parse_chapter(load("chapter_unedited.html"))
    check("chapter id", c["chapter_id"], 3809451)
    check("published", c["published_at"].isoformat(), "2026-08-13T16:51:56+00:00")
    # The whole reason positional selectors are banned in this module: this page
    # has ONE <li>, so li:nth-child(2) would read nothing or the wrong row.
    check("updated_at is None (meaningful NULL)", c["updated_at"], None)
    check("was_edited", c["was_edited"], False)
    check("word count excludes hidden injection", c["word_count"], 2167)


def test_hidden_injection():
    """
    RoyalRoad injects an anti-plagiarism sentence behind a per-request random
    class. Both the class name and the wording change on every fetch, so hashing
    the raw body makes every chapter look edited on every refresh -- which is
    exactly what happened on the first live run. This is the regression guard.
    """
    print("\nhidden anti-plagiarism injection")
    html = load("chapter_unedited.html")
    original = rr.parse_chapter(html)
    check("hidden nodes were found and removed", original["hidden_removed"] > 0, True)

    # Simulate the next fetch: RoyalRoad re-rolls the class AND the sentence.
    cls = re.search(r"\.([A-Za-z0-9]{20,})\{\s*display:\s*none", html).group(1)
    refetch = (html.replace(cls, "zzYWJjZGVmMDEyMzQ1Njc4OWFiY2RlZjAx")
                   .replace("If you stumble upon this narrative on Amazon",
                            "This tale has been unlawfully lifted from Royal Road"))
    second = rr.parse_chapter(refetch)
    check("hash survives re-randomised injection",
          second["content_hash"], original["content_hash"])
    check("word count survives it too",
          second["word_count"], original["word_count"])

    # A genuine edit must still register. Insert INSIDE the chapter body -- the
    # first </div> in the document is page furniture, not hashed content, so
    # appending there would prove nothing.
    marker = '<div class="chapter-inner chapter-content">'
    assert marker in html
    edited = html.replace(marker, marker + "<p>a real new sentence from the author</p>", 1)
    check("a real content change still moves the hash",
          rr.parse_chapter(edited)["content_hash"] != original["content_hash"], True)
    check("and is reflected in the word count",
          rr.parse_chapter(edited)["word_count"], original["word_count"] + 7)


def test_chapter_failure_modes():
    print("\nchapter failure modes")
    try:
        rr.parse_chapter("<html><body><h1>x</h1></body></html>")
        check("missing body raises", "no error", "ParseError")
    except rr.ParseError:
        check("missing body raises", "ParseError", "ParseError")

    stub = ('<html><body><div class="chapter-inner chapter-content">'
            "<p>text</p></div></body></html>")
    try:
        rr.parse_chapter(stub)
        check("missing timestamps raises", "no error", "ParseError")
    except rr.ParseError:
        check("missing timestamps raises", "ParseError", "ParseError")


def test_cross_consistency():
    print("\nToC and chapter pages agree")
    toc = rr.parse_toc(load("fiction.html"))
    by_id = {c["chapter_id"]: c for c in toc["chapters"]}
    for name in ("chapter_edited.html", "chapter_unedited.html"):
        page = rr.parse_chapter(load(name))
        entry = by_id.get(page["chapter_id"])
        check(f"{name}: present in ToC", entry is not None, True)
        if entry:
            check(f"{name}: titles match", page["title"], entry["title"])
            # The ToC `date` and the chapter page "Published" row are the same
            # fact from two sources. If they ever disagree, one parser is wrong.
            check(f"{name}: published dates agree",
                  page["published_at"], entry["published_at"])


def main():
    test_toc()
    test_toc_failure_modes()
    test_chapter_edited()
    test_chapter_unedited()
    test_hidden_injection()
    test_chapter_failure_modes()
    test_cross_consistency()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
