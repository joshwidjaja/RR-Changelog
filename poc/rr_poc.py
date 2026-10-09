#!/usr/bin/env python3
"""Proof of concept: print every chapter's last-updated date for a RoyalRoad fiction.

Milestone 1 of OUTLINE.md. The scraping itself lives in rrchangelog/scraper.py.

    python3 poc/rr_poc.py https://www.royalroad.com/fiction/21220/mother-of-learning --limit 5 -v
    python3 poc/rr_poc.py URL --save-html pages/        # keep raw HTML for inspection
    python3 poc/rr_poc.py URL --from-dir pages/         # re-parse saved pages offline
    python3 poc/rr_poc.py URL --fetcher browser         # always use Playwright
    python3 poc/rr_poc.py URL --cookie "cf_clearance=..." --user-agent "<your browser's UA>"
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rrchangelog.scraper import (  # noqa: E402
    BASE_URL, DEFAULT_BROWSER_PROFILE, CloudflareBlocked, Fiction, FetchError,
    PageSource, parse_chapter_dates, parse_fiction, parse_fiction_id,
)


# --------------------------------------------------------------------------- CLI


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


CLOUDFLARE_HELP = """\
RoyalRoad returned a Cloudflare challenge page instead of content.

Options:
  1. Use the Playwright fallback (needs: pip install playwright && python -m playwright install chromium).
     If the browser is still challenged, add --show-browser and complete the challenge in the window.
  2. Open the fiction in your normal browser, then copy:
       - the cf_clearance cookie (DevTools → Application → Cookies → royalroad.com)
       - your browser's exact User-Agent (DevTools console: navigator.userAgent)
     and rerun with:  --fetcher http --cookie "cf_clearance=..." --user-agent "..."
  3. Save the pages from your browser (Save Page As → HTML only) as
     fiction.html and chapter-<id>.html in a folder, then rerun with --from-dir.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print every chapter's last-updated date for a RoyalRoad fiction.")
    p.add_argument("url", help="fiction URL, chapter URL, or fiction ID")
    p.add_argument("--limit", type=int, help="only check the first N chapters")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    p.add_argument("--user-agent", help="override the User-Agent (default: curl_cffi's Chrome profile)")
    p.add_argument("--cookie", help='Cookie header, e.g. "cf_clearance=..."')
    p.add_argument("--fetcher", choices=("auto", "http", "browser"), default="auto",
                   help="auto: HTTP (curl_cffi), switching to Playwright if Cloudflare blocks it (default)")
    p.add_argument("--show-browser", action="store_true",
                   help="show the Playwright window (e.g. to solve a Cloudflare challenge by hand)")
    p.add_argument("--browser-profile", metavar="DIR", default=str(DEFAULT_BROWSER_PROFILE),
                   help="Playwright profile dir; keeps Cloudflare clearance between runs")
    p.add_argument("--save-html", metavar="DIR", help="save fetched pages to DIR")
    p.add_argument("--from-dir", metavar="DIR", help="read pages from DIR instead of the network")
    p.add_argument("--json", action="store_true", help="output JSON instead of a table")
    p.add_argument("-v", "--verbose", action="store_true", help="show every date candidate per chapter")
    args = p.parse_args(argv)

    try:
        fiction_id = parse_fiction_id(args.url)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING, format="\n%(message)s")
    source = PageSource(fetcher=args.fetcher, delay=args.delay, user_agent=args.user_agent, cookie=args.cookie,
                        show_browser=args.show_browser, browser_profile=Path(args.browser_profile),
                        from_dir=args.from_dir, save_dir=args.save_html)
    try:
        return run(args, fiction_id, source)
    finally:
        source.close()


def run(args, fiction_id: int, source: PageSource) -> int:
    try:
        fiction_html = source.get("fiction", f"{BASE_URL}/fiction/{fiction_id}")
        if fiction_html is None:
            print(f"fiction.html not found in {args.from_dir}", file=sys.stderr)
            return 1
        fiction = parse_fiction(fiction_html, fiction_id)
    except CloudflareBlocked:
        print(CLOUDFLARE_HELP, file=sys.stderr)
        return 2
    except (FetchError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    chapters = fiction.chapters[: args.limit] if args.limit else fiction.chapters
    log = (lambda *a: print(*a, file=sys.stderr)) if args.json else (lambda *a: print(*a, flush=True))
    log(f"{fiction.title} — {fiction.author}  ({len(fiction.chapters)} chapters, "
        f"fiction modified {fmt(fiction.date_modified)} UTC)")
    log(f"Checking {len(chapters)} chapter page(s)...\n")

    results = []
    try:
        for i, chapter in enumerate(chapters, 1):
            row = {"chapter": chapter, "dates": None, "note": ""}
            results.append(row)
            if chapter.locked:
                row["note"] = "locked (premium/advance)"
                continue
            try:
                body = source.get(f"chapter-{chapter.id}", chapter.url)
            except CloudflareBlocked:
                row["note"] = "blocked by Cloudflare"
                print(CLOUDFLARE_HELP, file=sys.stderr)
                break
            except FetchError as e:
                row["note"] = str(e)
                continue
            if body is None:
                row["note"] = "not in --from-dir"
                continue
            row["dates"] = parse_chapter_dates(body)
            if not args.json:
                print(f"\r  {i}/{len(chapters)}", end="", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted — showing partial results.", file=sys.stderr)
    if not args.json:
        print("\r" + " " * 20 + "\r", end="", file=sys.stderr, flush=True)

    if args.json:
        print(json.dumps(to_json(fiction, results), indent=2, default=str))
    else:
        print_table(results, args.verbose)
    return 0


def to_json(fiction: Fiction, results: list[dict]) -> dict:
    return {
        "fiction": {"id": fiction.id, "title": fiction.title, "author": fiction.author,
                    "date_modified": fiction.date_modified, "chapter_count": len(fiction.chapters)},
        "chapters": [{
            **asdict(r["chapter"]),
            "page_published": r["dates"].published.value if r["dates"] and r["dates"].published else None,
            "last_updated": r["dates"].updated.value if r["dates"] and r["dates"].updated else None,
            "last_updated_source": r["dates"].updated.source if r["dates"] and r["dates"].updated else None,
            "candidates": [asdict(c) for c in r["dates"].candidates] if r["dates"] else [],
            "note": r["note"],
        } for r in results],
    }


def print_table(results: list[dict], verbose: bool) -> None:
    print(f"{'#':>4}  {'Published (UTC)':<16}  {'Last updated':<16}  {'Source':<28}  Title")
    print("-" * 100)
    found = edited = 0
    for r in results:
        ch, dates = r["chapter"], r["dates"]
        updated = dates.updated if dates else None
        published = ch.published or (dates.published.value if dates and dates.published else None)
        if updated:
            found += 1
            if published and abs((updated.value - published).total_seconds()) > 60:
                edited += 1
        source = updated.source if updated else (r["note"] or "no 'updated' date found")
        print(f"{ch.order + 1:>4}  {fmt(published):<16}  {fmt(updated.value if updated else None):<16}  "
              f"{source[:28]:<28}  {ch.title}")
        if verbose and dates:
            for c in dates.candidates:
                print(f"{'':>6}· {c.kind:<9} {fmt(c.value)}  {c.source}")

    print(f"\n{found}/{len(results)} chapters had an 'updated' date; {edited} differ from publish date.")
    if any(r["dates"] and not r["dates"].updated for r in results):
        print("Chapters without one are most likely unedited (RoyalRoad omits 'Last Edit' then).")
    if results and not found:
        print("No 'updated' dates were recognised. Rerun with --save-html DIR -v and inspect the\n"
              "candidates/HTML to find where RoyalRoad shows it, then adjust parse_chapter_dates().")


if __name__ == "__main__":
    sys.exit(main())
