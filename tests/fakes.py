"""Offline stand-ins for RoyalRoad: generated fiction/chapter pages and a fake PageSource."""

import json
from datetime import datetime, timezone

from rrchangelog.scraper import CloudflareBlocked, FetchError

FICTION_ID = 21220  # Mother of Learning's ID, though the content here is synthetic


def ts(year, month, day, hour=12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def fiction_page(chapters: list[dict], title="Mother of Learning", author="nobody103") -> str:
    """chapters: dicts with id, title, published (unix), optional locked."""
    data = [{
        "id": c["id"], "volumeId": None, "title": c["title"], "slug": f"c{c['id']}",
        "date": datetime.fromtimestamp(c["published"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": i, "visible": 1, "subscriptionTiers": [1] if c.get("locked") else None,
        "url": f"/fiction/{FICTION_ID}/mother-of-learning/chapter/{c['id']}/c{c['id']}",
    } for i, c in enumerate(chapters)]
    ld = {"@type": "Book", "name": title, "author": {"@type": "Person", "name": author, "url": "/profile/1"}}
    return (f'<html><head><meta property="og:image" content="https://example.com/cover.jpg">'
            f'<script type="application/ld+json">{json.dumps(ld)}</script></head><body>'
            f"<script>\n  window.chapters = {json.dumps(data)};\n</script></body></html>")


def chapter_page(published: int, edited: int | None = None) -> str:
    """Legacy-layout chapter page markup (see tests/test_scraper.py CHAPTER_PAGE)."""
    edit = (f'<li><i class="fa fa-edit" title="Last Edit"></i> <time unixtime="{edited}" format="U">x</time></li>'
            if edited else "")
    return (f'<html><body><ul><li><i class="fa fa-calendar" title="Published"></i> '
            f'<time unixtime="{published}" format="U">x</time></li>{edit}</ul>'
            f'<div class="chapter-inner chapter-content"><p>Text.</p></div></body></html>')


class FakeSource:
    """Serves pages by name ("fiction", "chapter-<id>"); a value may be an exception to raise."""

    def __init__(self):
        self.pages: dict[str, object] = {}
        self.requests: list[str] = []
        self.closed = False

    def set_fiction(self, chapters: list[dict], **kwargs) -> None:
        """Set the fiction page and one chapter page per chapter (using each dict's 'edited')."""
        self.pages = {"fiction": fiction_page(chapters, **kwargs)}
        for c in chapters:
            self.pages[f"chapter-{c['id']}"] = chapter_page(c["published"], c.get("edited"))

    def get(self, name: str, url: str) -> str | None:
        self.requests.append(name)
        page = self.pages.get(name)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise FetchError(f"HTTP 404 for {url}")
        return page

    def close(self) -> None:
        self.closed = True


__all__ = ["FICTION_ID", "CloudflareBlocked", "FakeSource", "FetchError", "chapter_page", "fiction_page", "ts"]
