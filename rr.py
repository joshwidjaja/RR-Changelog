"""
RoyalRoad adapter — every site-specific selector in the project lives here.

The site's markup will change eventually. Keeping the whole blast radius in one
module means a future break is one file to fix, and the tests below fail loudly
against saved fixtures rather than silently producing wrong data.

Two page types:

  parse_toc(html)      fiction page  -> fiction metadata + full chapter list
  parse_chapter(html)  chapter page  -> published/edited timestamps + content hash

The ToC is the cheap call (one request, whole list). The chapter page is the
expensive one (one request each, and RoyalRoad sends no cache validators, so
there is no way to make it cheaper). See the project doc §4.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE = "https://www.royalroad.com"

# On a chapter page the <i title="..."> is the LABEL and the <time> is its next
# sibling. Key on the title and walk sideways -- never on position within the
# <ul>, because an unedited chapter omits the "Last Edit" <li> entirely, so
# li:nth-child(2) silently reads the wrong row or nothing at all.
TIME_LABELS = {"Published": "published_at", "Last Edit": "updated_at"}

FICTION_URL_RE = re.compile(r"/fiction/(\d+)(?:/([^/?#]+))?")
CHAPTER_URL_RE = re.compile(r"/fiction/(\d+)/[^/]+/chapter/(\d+)")


class ParseError(RuntimeError):
    """Raised when the markup no longer matches what the fixtures taught us."""


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------

def parse_rr_datetime(value):
    """
    RoyalRoad emits ISO-8601 UTC in two shapes:
        chapter page  '2026-07-28T14:16:32.2538450Z'   (SEVEN fractional digits)
        window.chapters '2026-09-06T16:19:31Z'         (no fractional part)

    Seven digits is the trap: datetime.fromisoformat accepts it only on Python
    3.11+, and raises ValueError on 3.10 and earlier. Trimming to six keeps this
    working on older interpreters too.
    """
    if not value:
        return None
    iso = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _time_from_unixtime(tag):
    """Fallback when the datetime attribute is missing or unparseable.

    Note `unixtime` is inconsistently typed across the two rows -- an int on
    Published ('1764170027'), a float on Last Edit ('1785248192.253845') -- so
    always coerce through float() rather than int().
    """
    raw = tag.get("unixtime")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# fiction page (table of contents)
# --------------------------------------------------------------------------

def parse_toc(html, url=None):
    """
    Parse a fiction page into {fiction: {...}, chapters: [...]}.

    RoyalRoad embeds the complete chapter list as JSON in a `window.chapters`
    script assignment. That's the source of truth here rather than the rendered
    <table>: it carries typed fields, it isn't paginated (117/117 chapters in
    one page for the fixture fiction), and it's far less brittle than scraping
    table cells. The HTML table exists too and would serve as a fallback, but
    two parsers for one fact is two things to keep working -- so this raises
    instead, and the tests will catch it.

    Each JSON entry looks like:
        {"id": 2794892, "volumeId": null, "title": "...", "slug": "...",
         "date": "2025-11-26T15:13:47Z", "order": 0, "visible": 1,
         "subscriptionTiers": null, "doesNotRollOver": false,
         "isUnlocked": true, "url": "/fiction/141517/..."}

    `date` is the PUBLISHED date. There is no edit information here -- that's
    the whole reason chapter pages have to be fetched individually.
    """
    m = re.search(r"window\.chapters\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        raise ParseError(
            "window.chapters not found. RoyalRoad may have changed the fiction "
            "page; re-run the Spike #1 inspection against a fresh fixture."
        )
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ParseError(f"window.chapters present but not valid JSON: {e}") from e

    soup = BeautifulSoup(html, "lxml")

    fid = re.search(r"window\.fictionId\s*=\s*(\d+)", html)
    fiction_id = int(fid.group(1)) if fid else None

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None

    # First /profile/ link with real text is the author; later ones are empty
    # wrappers around the avatar image.
    author = None
    for a in soup.select('a[href^="/profile/"]'):
        text = a.get_text(strip=True)
        if text:
            author = text
            break

    if fiction_id is None and url:
        mm = FICTION_URL_RE.search(url)
        fiction_id = int(mm.group(1)) if mm else None

    chapters = []
    for c in raw:
        chapters.append({
            "chapter_id": c["id"],
            "title": c.get("title", "").strip(),
            "url": urljoin(BASE, c.get("url", "")),
            # RoyalRoad's `order` is 0-based. Normalise to 1-based so that a
            # position in this tool lines up with how a reader counts chapters.
            "position": int(c.get("order", 0)) + 1,
            "published_at": parse_rr_datetime(c.get("date")),
            # Flags kept so the refresh loop can skip what it must not fetch.
            "visible": bool(c.get("visible", 1)),
            "is_unlocked": bool(c.get("isUnlocked", True)),
        })

    return {
        "fiction": {
            "rr_id": fiction_id,
            "title": title,
            "author": author,
            "url": url or (f"{BASE}/fiction/{fiction_id}" if fiction_id else None),
        },
        "chapters": chapters,
    }


def fetchable(chapters):
    """
    Chapters a refresh may actually request.

    `visible: 0` is a chapter hidden from the index; `isUnlocked: false` marks
    paid early access behind a subscription tier. Neither is fetchable
    anonymously, and hammering them produces errors, not data. All 117 chapters
    in the fixture fiction are visible and unlocked, so this path is untested
    against real locked content -- but the fields exist in RoyalRoad's schema
    and skipping them costs nothing.
    """
    return [c for c in chapters if c["visible"] and c["is_unlocked"]]


# --------------------------------------------------------------------------
# chapter page
# --------------------------------------------------------------------------

_HIDDEN_RULE = re.compile(
    r"\.([A-Za-z_][\w-]*)\s*\{[^}]*display\s*:\s*none[^}]*\}", re.I)
_INLINE_HIDDEN = re.compile(r"display\s*:\s*none", re.I)


def strip_hidden(soup):
    """
    Remove content RoyalRoad hides from readers, and return how many nodes went.

    RoyalRoad injects an anti-plagiarism sentence into every chapter body --
    "Stolen from its rightful author...", "If you stumble upon this narrative on
    Amazon..." -- concealed behind a per-request random class:

        <style>.cmMxZGNhZDgwYTFlODRlZTY4MjZlM2FhZGJkMjgwNGEw{display:none;speak:never;}</style>
        <p class="cmMxZGNhZDgwYTFlODRlZTY4MjZlM2FhZGJkMjgwNGEw">Stolen from ...</p>

    Both the class name AND the sentence are randomised per fetch, so hashing the
    raw body produces a different digest every single time. Left unhandled, every
    chapter reports as edited on every refresh and the changelog is worthless.
    This is the single most important normalisation step in the project.

    Rather than pattern-matching RoyalRoad's warning phrases -- a list that would
    rot the moment they add a new one -- read the page's own CSS for whatever it
    declares display:none and drop those nodes. That stays correct if the wording
    changes, and it also catches any other hidden injection.
    """
    hidden = set()
    for style in soup.find_all("style"):
        css = style.string or style.get_text() or ""
        hidden.update(_HIDDEN_RULE.findall(css))

    # Collect every target BEFORE removing any of them. Decomposing as we go
    # invalidates nodes that a later selector still matches -- a decomposed node
    # has attrs set to None, and touching it raises.
    targets = []
    for cls in hidden:
        try:
            targets.extend(soup.select(f".{cls}"))
        except Exception:                 # exotic class name; skip rather than crash
            continue
    for node in soup.select("[style]"):
        if _INLINE_HIDDEN.search(node.attrs.get("style", "") if node.attrs else ""):
            targets.append(node)

    removed = 0
    seen = set()
    for node in targets:
        if id(node) in seen or node.attrs is None:   # duplicate, or already gone
            continue
        seen.add(id(node))
        node.decompose()
        removed += 1

    return removed


def parse_chapter(html):
    """
    Parse a chapter page. Returns published/updated timestamps, identity, and a
    content fingerprint.

    `updated_at` is None when RoyalRoad omits the "Last Edit" row, which it does
    for any chapter that has never been edited. That None is MEANINGFUL and gets
    stored as SQL NULL -- it records "never edited", not "unknown".
    """
    soup = BeautifulSoup(html, "lxml")
    out = {"published_at": None, "updated_at": None}

    for icon in soup.select("i[title]"):
        field = TIME_LABELS.get(icon.get("title", ""))
        if not field:
            continue
        tag = icon.find_next_sibling("time")
        if tag is not None:
            out[field] = parse_rr_datetime(tag.get("datetime")) or _time_from_unixtime(tag)

    og = soup.find("meta", property="og:url")
    url = og["content"] if og and og.get("content") else ""
    ids = CHAPTER_URL_RE.search(url)
    out["fiction_id"] = int(ids.group(1)) if ids else None
    out["chapter_id"] = int(ids.group(2)) if ids else None
    out["url"] = url

    h1 = soup.find("h1")
    out["title"] = h1.get_text(strip=True) if h1 else None

    # Must happen BEFORE the body is read: RoyalRoad's injected anti-plagiarism
    # line is randomised per request, so hashing without this makes every chapter
    # look edited on every refresh.
    out["hidden_removed"] = strip_hidden(soup)

    body = soup.select_one("div.chapter-inner.chapter-content")
    if body is None:
        raise ParseError("chapter body (div.chapter-inner.chapter-content) not found")

    # Normalise before hashing so a cosmetic re-render doesn't read as an edit.
    # Only the fingerprint is kept -- never the text itself. RoyalRoad's
    # robots.txt blocks AI training crawlers wholesale, and storing chapter
    # prose would make this tool the thing they're objecting to.
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
    out["word_count"] = len(text.split())
    out["content_hash"] = hashlib.sha256(text.encode()).hexdigest()[:16]

    if out["published_at"] is None and out["updated_at"] is None:
        raise ParseError("no Published/Last Edit timestamps found on chapter page")

    out["was_edited"] = out["updated_at"] is not None
    return out
