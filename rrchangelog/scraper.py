"""RoyalRoad scraping: fetch fiction/chapter pages and extract chapter dates.

Shared by the web app (rrchangelog.sync) and the PoC CLI (poc/rr_poc.py).

Chapter pages show dates next to Font Awesome icons in both layouts (verified 2026-10-09):
    legacy:   <i class="fa fa-calendar" title="Published"></i> <time unixtime=...>
              <i class="fa fa-edit" title="Last Edit"></i> <time unixtime=...>
    redesign: <i class="fa fa-calendar mr-1"></i> <time unixtime=...> ... tooltip "Created At"
              <i class="fa fa-edit mr-1"></i> <time unixtime=...> ... tooltip "Edited At"
The edit entry appears only on edited chapters. Each <time> is classified by its icon
first, then by nearby labels, and the source used is reported alongside the date.
"""

import gzip
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://www.royalroad.com"
DEFAULT_USER_AGENT = "RR-Changelog/0.2 (personal chapter-date checker)"
DEFAULT_BROWSER_PROFILE = Path(__file__).resolve().parent.parent / ".browser-profile"

log = logging.getLogger(__name__)


def _ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without system certificates; certifi fixes that when present.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = _ssl_context()

UPDATED_WORDS = ("updat", "edit", "modif", "revis")
PUBLISHED_WORDS = ("publish", "posted", "released", "created")
ICON_KINDS = {"fa-edit": "updated", "fa-pen-to-square": "updated", "fa-calendar": "published"}


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
    cover_url: str = ""


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
    icon: str  # kind implied by the closest preceding Font Awesome icon, or ""
    labels: str  # title/aria-label attributes of tags just before this <time>
    preceding: str  # visible text just before this <time>
    text: str = ""
    trailing: str = ""  # visible text right after it (redesign tooltips: "Edited At")


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
        self._recent_icon = ""
        self._open_time: TimeTag | None = None
        self._last_time: TimeTag | None = None
        self._table_depth = 0
        self._chapters_table_depth: int | None = None
        self._row: dict | None = None

    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}

        label = " ".join(attrs[k] for k in ("title", "aria-label", "data-original-title") if attrs.get(k))
        if label and tag != "time":  # a <time>'s own title is just the formatted date
            self._recent_labels = (self._recent_labels + [label])[-3:]

        if tag == "i":
            # Only the closest icon counts, so an unrelated icon resets it.
            classes = attrs.get("class", "").split()
            self._recent_icon = next((ICON_KINDS[c] for c in classes if c in ICON_KINDS), "")

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
            self._open_time = TimeTag(
                attrs=attrs,
                icon=self._recent_icon,
                labels=" | ".join(self._recent_labels),
                preceding=self._recent_text.strip(),
            )
            self.times.append(self._open_time)
            self._last_time = None
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
            self._last_time = self._open_time
            self._open_time = None
            self._recent_text = ""
            self._recent_labels = []
            self._recent_icon = ""
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
        text = " ".join(data.split())
        claimed = False
        if self._open_time is not None:
            self._open_time.text += data
        elif self._last_time is not None and len(self._last_time.trailing) < self.CONTEXT_CHARS:
            # Claimed as this <time>'s trailing label, so it doesn't also count as the next one's context.
            self._last_time.trailing = (self._last_time.trailing + " " + text).strip()
            claimed = True
        if self._row is not None and self._row["href"] and not self._row["title"]:
            self._row["title"] = data.strip()
        if text and not claimed:
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


def classify_time(tag: TimeTag) -> tuple[str, str]:
    """Most specific evidence first: own label, icon, preceding labels, trailing tooltip, preceding text."""
    own = " ".join(tag.attrs[k] for k in ("aria-label", "data-original-title", "title") if tag.attrs.get(k))
    # Name the icon by whichever nearby label agrees with it ("Last Edit", "Edited At").
    nearest = (tag.labels.rsplit(" | ", 1)[-1], " ".join(tag.trailing.split()[:2]))
    icon_label = next((t for t in nearest if tag.icon and classify(t) == tag.icon), "")
    for kind, source in (
        (classify(own), f'title "{own[:30]}"'),
        (tag.icon or "unlabeled", f'icon "{icon_label.strip()}"' if icon_label else "icon"),
        (classify(tag.labels), f'label "{tag.labels[-30:]}"'),
        (classify(tag.trailing), f'tooltip "{tag.trailing[:30]}"'),
        (classify(tag.preceding), f'text "{tag.preceding[-30:]}"'),
    ):
        if kind != "unlabeled":
            return kind, source
    return "unlabeled", f'<time> "{tag.text.strip()[:30]}"'


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
        cover_url=scanner.metas.get("og:image", ""),
    )


def parse_chapter_dates(html_text: str) -> ChapterDates:
    scanner = scan(html_text)
    candidates: list[DateCandidate] = []

    # 1. Visible <time> elements, classified by nearby labels ("Last updated", title="Published", ...)
    for tag in scanner.times:
        value = time_tag_value(tag)
        if value is None:
            continue
        kind, source = classify_time(tag)
        candidates.append(DateCandidate(kind, value, source))

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
#
# Cloudflare in front of RoyalRoad decides mostly on the client's TLS/HTTP fingerprint.
# Verified 2026-10-09 on Mother of Learning:
#   - Python urllib and curl: challenged on every request.
#   - curl_cffi impersonating Chrome: 109/109 chapters (twice), no challenge, no cookie needed.
#   - Real (headed) Chromium: passes; kept as a fallback in case the fingerprint check tightens.

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # optional; fall back to urllib
    cffi_requests = None


def fetch(url: str, user_agent: str, cookie: str | None) -> str:
    """Plain urllib fetch. Used only when curl_cffi isn't installed."""
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
        with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as resp:
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


class HttpFetcher:
    """HTTP without a browser: curl_cffi impersonating Chrome when installed, else urllib.

    With curl_cffi the User-Agent is left to the impersonation profile unless one is
    given explicitly, since a UA that doesn't match the TLS fingerprint looks suspicious.
    """

    def __init__(self, user_agent: str | None = None, cookie: str | None = None):
        self.user_agent, self.cookie = user_agent, cookie
        self._session = cffi_requests.Session(impersonate="chrome") if cffi_requests else None

    @property
    def name(self) -> str:
        return "curl_cffi (Chrome fingerprint)" if self._session else "urllib"

    def fetch(self, url: str) -> str:
        if self._session is None:
            return fetch(url, self.user_agent or DEFAULT_USER_AGENT, self.cookie)
        headers = {}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            response = self._session.get(url, headers=headers, timeout=30)
        except Exception as e:  # curl_cffi's error classes moved between versions
            raise FetchError(f"{e} for {url}") from None
        body = response.text
        if is_cloudflare_challenge(body):
            raise CloudflareBlocked(url)
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        return body

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class BrowserFetcher:
    """Fallback: real Chromium via Playwright, for when Cloudflare blocks HttpFetcher too.

    The window is navigated only to get past Cloudflare: once a RoyalRoad page has
    loaded, every chapter is downloaded with fetch() from inside that page, so the
    browser's cookies and fingerprint are reused without rendering each chapter.
    fetch() also returns the raw server HTML, untouched by RoyalRoad's scripts.

    Verified 2026-10-09: headless Chromium gets challenged after ~2 pages, a headed one
    isn't, so this runs headed with the window parked off-screen unless show=True (to
    solve a challenge by hand). A persistent profile keeps cf_clearance between runs.
    """

    # Only RoyalRoad itself and Cloudflare's challenge need to load. Blocking the rest
    # (ads, analytics, web fonts, images) keeps the browser from pegging a CPU core.
    ALLOWED_HOSTS = ("royalroad.com", "cloudflare.com")
    BLOCKED_RESOURCES = {"image", "media", "font"}
    TIMEOUT_MS = 30_000
    SLOW_FETCH_SECONDS = 20
    IN_PAGE_FETCH = """async (url) => {
        const response = await fetch(url, { credentials: "include", redirect: "follow" });
        return [response.status, await response.text()];
    }"""

    def __init__(self, show: bool, profile_dir: Path):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise FetchError("Playwright isn't installed: pip install playwright && "
                             "python -m playwright install chromium") from None
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if not show:
            args.append("--window-position=-32000,-32000")
        # Leave a person time to click through a challenge when the window is visible.
        self.challenge_timeout_ms = 180_000 if show else 30_000
        self._pw = sync_playwright().start()
        try:
            self._context = self._pw.chromium.launch_persistent_context(str(profile_dir), headless=False, args=args)
        except Exception:
            self._pw.stop()
            raise
        self._context.set_default_timeout(self.TIMEOUT_MS)
        self._context.route("**/*", self._route)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._on_royalroad = False

    def _route(self, route) -> None:
        request = route.request
        host = urlsplit(request.url).hostname or ""
        allowed_host = any(host == h or host.endswith("." + h) for h in self.ALLOWED_HOSTS)
        if allowed_host and request.resource_type not in self.BLOCKED_RESOURCES:
            route.continue_()
        else:
            route.abort()

    def fetch(self, url: str) -> str:
        started = time.monotonic()
        try:
            return self._fetch(url)
        finally:
            elapsed = time.monotonic() - started
            (log.warning if elapsed > self.SLOW_FETCH_SECONDS else log.debug)("browser fetch took %.1fs: %s", elapsed, url)

    def _fetch(self, url: str) -> str:
        from playwright.sync_api import Error as PlaywrightError
        try:
            if not self._on_royalroad:
                return self._navigate(url)  # first request: load a real page to get past Cloudflare
            status, body = self._page.evaluate(self.IN_PAGE_FETCH, url)
            if is_cloudflare_challenge(body):
                # Clearance expired or Cloudflare escalated: solve it in the window again.
                return self._navigate(url)
        except PlaywrightError as e:
            self._on_royalroad = False  # e.g. the page navigated away; reload on the next try
            raise FetchError(f"{e.message.splitlines()[0]} for {url}") from None
        if status >= 400:
            raise FetchError(f"HTTP {status} for {url}")
        return body

    def _navigate(self, url: str) -> str:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        log.debug("browser navigating to %s", url)
        response = self._page.goto(url, wait_until="domcontentloaded")
        if response is not None and is_cloudflare_challenge(response.text()):
            # The interstitial solves itself in a real browser; once it's gone, reload for raw HTML.
            try:
                self._page.wait_for_function("document.title !== 'Just a moment...'",
                                             timeout=self.challenge_timeout_ms)
            except PlaywrightTimeout:
                raise CloudflareBlocked(url) from None
            response = self._page.goto(url, wait_until="domcontentloaded")
        if response is None:
            raise FetchError(f"No response for {url}")
        body = response.text()
        if is_cloudflare_challenge(body):
            raise CloudflareBlocked(url)
        if response.status >= 400:
            raise FetchError(f"HTTP {response.status} for {url}")
        self._on_royalroad = True
        return body

    def close(self) -> None:
        self._context.close()
        self._pw.stop()


class PageSource:
    """Fetches pages live (throttled, with retries), or reads them from a directory saved earlier.

    fetcher="auto" uses HttpFetcher and switches to BrowserFetcher for the rest of this
    PageSource's life the first time Cloudflare blocks a request. Playwright objects
    are thread-bound, so use a PageSource from one thread only.
    """

    def __init__(self, *, fetcher: str = "auto", delay: float = 1.0, user_agent: str | None = None,
                 cookie: str | None = None, show_browser: bool = False,
                 browser_profile: Path = DEFAULT_BROWSER_PROFILE,
                 from_dir: Path | None = None, save_dir: Path | None = None):
        if fetcher not in ("auto", "http", "browser"):
            raise ValueError(f"unknown fetcher: {fetcher}")
        self.fetcher, self.delay = fetcher, delay
        self.show_browser, self.browser_profile = show_browser, Path(browser_profile)
        self.from_dir = Path(from_dir) if from_dir else None
        self.save_dir = Path(save_dir) if save_dir else None
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        self._last_fetch = 0.0
        self._http = HttpFetcher(user_agent, cookie)
        self._browser: BrowserFetcher | None = None
        self._use_browser = fetcher == "browser"

    def close(self) -> None:
        self._http.close()
        if self._browser:
            self._browser.close()
            self._browser = None

    def get(self, name: str, url: str) -> str | None:
        if self.from_dir:
            path = self.from_dir / f"{name}.html"
            return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

        wait = self.delay - (time.monotonic() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        try:
            body = self._fetch_with_retry(url)
        finally:
            self._last_fetch = time.monotonic()
        if self.save_dir:
            (self.save_dir / f"{name}.html").write_text(body, encoding="utf-8")
        return body

    def _fetch_once(self, url: str) -> str:
        if not self._use_browser:
            try:
                return self._http.fetch(url)
            except CloudflareBlocked:
                if self.fetcher != "auto":
                    raise
                log.warning("Cloudflare blocked %s; switching to a browser (Playwright).", self._http.name)
                self._use_browser = True
        if self._browser is None:
            self._browser = BrowserFetcher(self.show_browser, self.browser_profile)
        return self._browser.fetch(url)

    def _fetch_with_retry(self, url: str, attempts: int = 3) -> str:
        for attempt in range(1, attempts + 1):
            try:
                return self._fetch_once(url)
            except FetchError:
                if attempt == attempts:
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")
