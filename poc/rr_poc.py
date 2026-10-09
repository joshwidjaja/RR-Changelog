#!/usr/bin/env python3
"""Proof of concept: print every chapter's last-updated date for a RoyalRoad fiction.

Milestone 1 of OUTLINE.md. Standard library only.

    python3 poc/rr_poc.py https://www.royalroad.com/fiction/21220/mother-of-learning --limit 5 -v
    python3 poc/rr_poc.py URL --save-html pages/        # keep raw HTML for inspection
    python3 poc/rr_poc.py URL --from-dir pages/         # re-parse saved pages offline
    python3 poc/rr_poc.py URL --cookie "cf_clearance=..." --user-agent "<your browser's UA>"

Chapter pages (legacy layout, verified 2026-10-09) show dates as
    <i class="fa fa-calendar" title="Published"></i> <time unixtime=...>
    <i class="fa fa-edit" title="Last Edit"></i> <time unixtime=...>
with the "Last Edit" entry apparently present only on edited chapters. The redesign
may differ, so the parser classifies every date candidate by its label and reports
which one it used (the Source column). Use -v to see all candidates.
"""

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://www.royalroad.com"
DEFAULT_USER_AGENT = "RR-Changelog-PoC/0.1 (personal chapter-date checker)"

UPDATED_WORDS = ("updat", "edit", "modif", "revis")
PUBLISHED_WORDS = ("publish", "posted", "released")


class CloudflareBlocked(Exception):
    pass


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------- models


@dataclass
class Chapter:
    id: int
    title: str
    url: str
    order: int
    published: datetime | None = None
    locked: bool = False


@dataclass
class Fiction:
    id: int
    title: str
    author: str
    chapters: list[Chapter]
    date_modified: datetime | None = None


@dataclass
class DateCandidate:
    kind: str  # "updated" | "published" | "unlabeled"
    value: datetime
    source: str


@dataclass
class ChapterDates:
    published: DateCandidate | None = None
    updated: DateCandidate | None = None
    candidates: list[DateCandidate] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def parse_fiction_id(url: str) -> int:
    """Accept fiction URLs, chapter URLs, or a bare fiction ID."""
    url = url.strip()
    if url.isdigit():
        return int(url)
    m = re.search(r"royalroad\.com/fiction/(\d+)", url, re.IGNORECASE)
    if not m:
        raise ValueError(f"Not a RoyalRoad fiction URL: {url}")
    return int(m.group(1))


def parse_timestamp(value: str | int | float | None) -> datetime | None:
    """Parse a unix timestamp or ISO-8601 string into an aware UTC datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_cloudflare_challenge(body: str) -> bool:
    # Normal Cloudflare-proxied pages may include /cdn-cgi/challenge-platform/
    # beacon scripts, so only match markers unique to the interstitial page.
    return "<title>Just a moment...</title>" in body or "window._cf_chl_opt" in body


def classify(text: str) -> str:
    text = text.lower()
    if any(w in text for w in UPDATED_WORDS):
        return "updated"
    if any(w in text for w in PUBLISHED_WORDS):
        return "published"
    return "unlabeled"


# --------------------------------------------------------------------------- HTML scanning


@dataclass
class TimeTag:
    attrs: dict[str, str]
    context: str  # nearby labels: preceding text plus title/aria-label of recent tags
    text: str = ""


class PageScanner(HTMLParser):
    """Collects the bits of a RoyalRoad page we care about in a single pass."""

    CONTEXT_CHARS = 80

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.scripts: list[tuple[str, str]] = []  # (type attribute, contents)
        self.metas: dict[str, str] = {}
        self.times: list[TimeTag] = []
        self.chapter_rows: list[dict] = []  # legacy table#chapters rows

        self._in_title = False
        self._script_type: str | None = None
        self._script_buf: list[str] = []
        self._skip_text = 0  # inside <style>
        self._recent_text = ""
        self._recent_labels: list[str] = []
        self._open_time: TimeTag | None = None
        self._table_depth = 0
        self._chapters_table_depth: int | None = None
        self._row: dict | None = None

    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}

        label = " ".join(attrs[k] for k in ("title", "aria-label", "data-original-title") if attrs.get(k))
        if label:
            self._recent_labels = (self._recent_labels + [label])[-3:]

        if tag == "title":
            self._in_title = True
        elif tag == "script":
            self._script_type = attrs.get("type", "")
            self._script_buf = []
        elif tag == "style":
            self._skip_text += 1
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
            if key and "content" in attrs:
                self.metas.setdefault(key, attrs["content"])
        elif tag == "table":
            self._table_depth += 1
            if attrs.get("id") == "chapters" and self._chapters_table_depth is None:
                self._chapters_table_depth = self._table_depth
        elif tag == "tr" and self._chapters_table_depth is not None:
            self._row = {"href": None, "title": "", "time": None}
            self.chapter_rows.append(self._row)
        elif tag == "a" and self._row is not None and self._row["href"] is None:
            href = attrs.get("href", "")
            if "/chapter/" in href:
                self._row["href"] = href
        elif tag == "time":
            context = " ".join(self._recent_labels) + " " + self._recent_text
            self._open_time = TimeTag(attrs=attrs, context=context.strip())
            self.times.append(self._open_time)
            if self._row is not None and self._row["time"] is None:
                self._row["time"] = attrs

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._script_type is not None:
            self.scripts.append((self._script_type, "".join(self._script_buf)))
            self._script_type = None
        elif tag == "style" and self._skip_text:
            self._skip_text -= 1
        elif tag == "time":
            # Labels seen so far belonged to this <time>; don't let them leak onto the next one.
            self._open_time = None
            self._recent_text = ""
            self._recent_labels = []
        elif tag == "tr":
            self._row = None
        elif tag == "table":
            if self._chapters_table_depth == self._table_depth:
                self._chapters_table_depth = None
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data):
        if self._script_type is not None:
            self._script_buf.append(data)
            return
        if self._skip_text:
            return
        if self._in_title:
            self.title += data
        if self._open_time is not None:
            self._open_time.text += data
        if self._row is not None and self._row["href"] and not self._row["title"]:
            self._row["title"] = data.strip()
        text = " ".join(data.split())
        if text:
            self._recent_text = (self._recent_text + " " + text)[-self.CONTEXT_CHARS:]


def scan(html_text: str) -> PageScanner:
    scanner = PageScanner()
    scanner.feed(html_text)
    scanner.close()
    return scanner


def json_ld_objects(scanner: PageScanner) -> list[dict]:
    objects = []
    for script_type, body in scanner.scripts:
        if script_type.lower() != "application/ld+json":
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict):
                objects.append(obj)
                objects.extend(o for o in obj.get("@graph", []) if isinstance(o, dict))
    return objects


def extract_window_chapters(scanner: PageScanner) -> list[dict] | None:
    decoder = json.JSONDecoder()
    for _, body in scanner.scripts:
        m = re.search(r"window\.chapters\s*=\s*", body)
        if not m:
            continue
        try:
            data, _ = decoder.raw_decode(body, m.end())
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def time_tag_value(tag: TimeTag) -> datetime | None:
    return parse_timestamp(tag.attrs.get("unixtime")) or parse_timestamp(tag.attrs.get("datetime"))


# --------------------------------------------------------------------------- parsers


def parse_fiction(html_text: str, fiction_id: int) -> Fiction:
    scanner = scan(html_text)
    ld = next((o for o in json_ld_objects(scanner) if o.get("name")), {})

    title = ld.get("name") or scanner.metas.get("og:title") or scanner.title.split("|")[0]
    author = ld.get("author", "")
    if isinstance(author, dict):
        author = author.get("name", "")
    author = author or scanner.metas.get("books:author", "")

    chapters: list[Chapter] = []
    raw = extract_window_chapters(scanner)
    if raw is not None:
        for i, c in enumerate(raw):
            chapters.append(Chapter(
                id=int(c["id"]),
                title=c.get("title", ""),
                url=BASE_URL + c["url"] if c.get("url", "").startswith("/") else c.get("url", ""),
                order=int(c.get("order", i)),
                published=parse_timestamp(c.get("date")),
                locked=bool(c.get("subscriptionTiers")),
            ))
    else:
        for i, row in enumerate(r for r in scanner.chapter_rows if r["href"]):
            m = re.search(r"/chapter/(\d+)", row["href"])
            if not m:
                continue
            published = None
            if row["time"]:
                published = parse_timestamp(row["time"].get("unixtime")) or parse_timestamp(row["time"].get("datetime"))
            chapters.append(Chapter(
                id=int(m.group(1)),
                title=row["title"],
                url=BASE_URL + row["href"] if row["href"].startswith("/") else row["href"],
                order=i,
                published=published,
            ))

    if not chapters:
        raise ValueError("No chapters found on fiction page (layout changed, or not a fiction page?)")
    chapters.sort(key=lambda c: c.order)

    return Fiction(
        id=fiction_id,
        title=" ".join(str(title).split()),
        author=str(author).strip(),
        chapters=chapters,
        date_modified=parse_timestamp(ld.get("dateModified")),
    )


def parse_chapter_dates(html_text: str) -> ChapterDates:
    scanner = scan(html_text)
    candidates: list[DateCandidate] = []

    # 1. Visible <time> elements, classified by nearby labels ("Last updated", title="Published", ...)
    for tag in scanner.times:
        value = time_tag_value(tag)
        if value is None:
            continue
        own_label = " ".join(tag.attrs[k] for k in ("title", "aria-label", "data-original-title") if tag.attrs.get(k))
        kind = classify(own_label)
        if kind == "unlabeled":
            kind = classify(tag.context)
        snippet = (own_label or tag.context)[-40:].strip()
        candidates.append(DateCandidate(kind, value, f'<time> "{snippet}"'))

    # 2. Meta tags
    for key, kind in (("article:modified_time", "updated"), ("og:updated_time", "updated"),
                      ("dateModified", "updated"), ("article:published_time", "published"),
                      ("datePublished", "published")):
        value = parse_timestamp(scanner.metas.get(key))
        if value:
            candidates.append(DateCandidate(kind, value, f"meta {key}"))

    # 3. JSON-LD
    for obj in json_ld_objects(scanner):
        for key, kind in (("dateModified", "updated"), ("datePublished", "published")):
            value = parse_timestamp(obj.get(key))
            if value:
                candidates.append(DateCandidate(kind, value, f"json-ld {obj.get('@type', '?')}.{key}"))

    return ChapterDates(
        published=next((c for c in candidates if c.kind == "published"), None),
        updated=next((c for c in candidates if c.kind == "updated"), None),
        candidates=candidates,
    )


# --------------------------------------------------------------------------- fetching


def fetch(url: str, user_agent: str, cookie: str | None) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)

    def decode(resp_body: bytes, encoding: str | None) -> str:
        if encoding == "gzip":
            resp_body = gzip.decompress(resp_body)
        return resp_body.decode("utf-8", errors="replace")

    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            body = decode(resp.read(), resp.headers.get("Content-Encoding"))
    except urllib.error.HTTPError as e:
        body = decode(e.read(), e.headers.get("Content-Encoding"))
        if is_cloudflare_challenge(body):
            raise CloudflareBlocked(url) from None
        raise FetchError(f"HTTP {e.code} for {url}") from None
    except urllib.error.URLError as e:
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
            hint = ("\n  python.org builds on macOS need certificates installed: run "
                    "'/Applications/Python 3.x/Install Certificates.command',\n"
                    "  or set SSL_CERT_FILE=/etc/ssl/cert.pem")
        raise FetchError(f"{e.reason} for {url}{hint}") from None

    if is_cloudflare_challenge(body):
        raise CloudflareBlocked(url)
    return body


class PageSource:
    """Fetches pages live (throttled), or reads them from a directory saved earlier."""

    def __init__(self, args):
        self.args = args
        self.from_dir = Path(args.from_dir) if args.from_dir else None
        self.save_dir = Path(args.save_html) if args.save_html else None
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        self._last_fetch = 0.0

    def get(self, name: str, url: str) -> str | None:
        if self.from_dir:
            path = self.from_dir / f"{name}.html"
            return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

        wait = self.args.delay - (time.monotonic() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        try:
            body = self._fetch_with_retry(url)
        finally:
            self._last_fetch = time.monotonic()
        if self.save_dir:
            (self.save_dir / f"{name}.html").write_text(body, encoding="utf-8")
        return body

    def _fetch_with_retry(self, url: str, attempts: int = 3) -> str:
        for attempt in range(1, attempts + 1):
            try:
                return fetch(url, self.args.user_agent, self.args.cookie)
            except FetchError:
                if attempt == attempts:
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")


# --------------------------------------------------------------------------- CLI


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


CLOUDFLARE_HELP = """\
RoyalRoad returned a Cloudflare challenge page instead of content.

Options:
  1. Open the fiction in your normal browser, then copy:
       - the cf_clearance cookie (DevTools → Application → Cookies → royalroad.com)
       - your browser's exact User-Agent (DevTools console: navigator.userAgent)
     and rerun with:  --cookie "cf_clearance=..." --user-agent "..."
  2. Save the pages from your browser (Save Page As → HTML only) as
     fiction.html and chapter-<id>.html in a folder, then rerun with --from-dir.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print every chapter's last-updated date for a RoyalRoad fiction.")
    p.add_argument("url", help="fiction URL, chapter URL, or fiction ID")
    p.add_argument("--limit", type=int, help="only check the first N chapters")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.add_argument("--cookie", help='Cookie header, e.g. "cf_clearance=..."')
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

    source = PageSource(args)
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
    if found < len(results):
        print("Chapters without one are most likely unedited (RoyalRoad omits 'Last Edit' then).")
    if results and not found:
        print("No 'updated' dates were recognised. Rerun with --save-html DIR -v and inspect the\n"
              "candidates/HTML to find where RoyalRoad shows it, then adjust parse_chapter_dates().")


if __name__ == "__main__":
    sys.exit(main())
