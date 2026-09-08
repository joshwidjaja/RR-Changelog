"""
Polite HTTP client for RoyalRoad.

Design constraints, all measured rather than assumed (see the Spike #2 doc):

  * RoyalRoad sends no cache validators on chapter pages -- no ETag, no
    Last-Modified, and Cache-Control: no-cache, no-store. Conditional requests
    are therefore impossible and every check costs a full ~105 KB body. There is
    no clever way around this; the only lever is fetching fewer chapters.
  * Measured latency is ~0.8 s per request. At a 1 req/s floor that's ~1.8 s per
    chapter, so a 300-chapter sweep runs ~9 minutes and moves ~31 MB.
  * robots.txt permits chapter paths for a general client. It also blocks AI
    training crawlers (GPTBot, CCBot, ClaudeBot, ...) wholesale.

That last point sets the rule this module follows: identify honestly, and do NOT
impersonate a browser. The reason this tool is acceptable is that it isn't the
thing RoyalRoad is objecting to -- disguising it would make that false.
"""

import time

import httpx

BASE = "https://www.royalroad.com"

USER_AGENT = (
    "RR-Changelog/0.1 (personal chapter-update tracker; "
    "+https://github.com/YOURNAME/rr-changelog)"
)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


class Fetcher:
    """
    Rate-limited client. One instance per run so the connection is reused --
    over hundreds of sequential requests, TLS handshakes are not free.
    """

    def __init__(self, min_interval=1.0, timeout=30.0, max_retries=3, user_agent=USER_AGENT):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
            timeout=timeout,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    def _throttle(self):
        """Sleep only for the time actually still owed, not a flat delay."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def get(self, url):
        """GET with backoff. Returns response text, or raises FetchError."""
        if url.startswith("/"):
            url = BASE + url

        last = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                r = self._client.get(url)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    raise FetchError(f"404 Not Found: {url}")   # never retry
                if r.status_code not in RETRY_STATUS:
                    raise FetchError(f"HTTP {r.status_code}: {url}")
                last = f"HTTP {r.status_code}"
                # Honour Retry-After when the server states a wait explicitly.
                after = r.headers.get("retry-after")
                if after and after.isdigit():
                    time.sleep(min(int(after), 60))

            if attempt < self.max_retries:
                time.sleep(2 ** attempt)      # 1s, 2s, 4s

        raise FetchError(f"gave up after {self.max_retries + 1} attempts ({last}): {url}")
