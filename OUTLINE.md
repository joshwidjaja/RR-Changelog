# RR-Changelog — Project Outline

A web app for viewing the **last-updated date of every chapter** in a RoyalRoad fiction without opening each chapter page. Users add fictions to a library by URL and refresh them to re-check every chapter and pick up new ones.

---

## 1. Architecture

Browsers cannot fetch `royalroad.com` directly (CORS), so scraping must run outside the page:

- **Backend server (chosen)** — the server fetches/parses RoyalRoad; the frontend only talks to our API.
- *Alternative:* a browser extension, which avoids CORS and uses the user's own session (and their Cloudflare clearance — see §2).

## 2. Scraping layer

### Known findings (2026-10-09)

- **Cloudflare is inconsistent.** `curl` (and WebFetch) got a `403` "Just a moment..." challenge, while Python `urllib` from the same machine got through. It likely depends on the client's TLS fingerprint, so it can change without warning.
- **Cloudflare fallback: Playwright (chosen).** Findings from testing:
  - Headless Chromium (either build) is challenged immediately while it reports `HeadlessChrome` in its UA.
  - With a normal UA it loads ~2 pages, then gets a challenge it never solves (resource blocking made no difference).
  - **Headed Chromium is not challenged at all.** It still works with the window parked off-screen (`--window-position=-32000,-32000`): a full 126-chapter fiction ran with zero challenges (≈2.7 s/chapter).
  - So the fetcher is: plain HTTP first → on the first challenge, switch to off-screen headed Chromium for the rest of the run, with a persistent profile so `cf_clearance` survives between runs. A "show browser" mode allows solving a challenge by hand.
  - Deployment implication: headed Chromium needs a display. Fine for a local app; a server needs `xvfb` (or similar).
  - Detect challenges and surface them; never mistake one for "no chapters" or "never edited".
- **Two RoyalRoad layouts are live.** A redesign has been in testing since Sept 2026 alongside the legacy UI. Parsers must handle both.
- **Fiction page data sources** (per FanFicFare's maintained adapter):
  - `window.chapters = [...]` JSON in a `<script>`: `id, volumeId, title, slug, date (ISO, published), order, visible, subscriptionTiers, isUnlocked, url`. Present in both layouts.
  - `<script type="application/ld+json">` (redesign): `name, author.name, author.url, description, datePublished, dateModified`.
  - Legacy fallback: `table#chapters tbody tr` rows with a chapter link and `<time unixtime="...">`.
  - Chapters with non-empty `subscriptionTiers` are premium/advance and not publicly readable.
- **Chapter page dates (verified on both layouts):**
  ```html
  <!-- legacy: label is the icon's title, before the <time> -->
  <i class="fa fa-calendar" title="Published"></i> <time unixtime="1540762483" …>…</time>
  <i class="fa fa-edit" title="Last Edit"></i> <time unixtime="1638566602" …>…</time>
  <!-- redesign: no icon title; label is a tooltip *after* the <time> -->
  <i class="fa fa-calendar mr-1"></i> <time unixtime="1764170027" …>…</time> … <div role="tooltip">Created At</div>
  <i class="fa fa-edit mr-1"></i> <time unixtime="1785248192" …>…</time> … <div role="tooltip">Edited At</div>
  ```
  - The **icon** (`fa-calendar` = published, `fa-edit` = edited) is the one signal common to both layouts, so the parser keys on it first, then falls back to nearby labels.
  - The edit entry appears only on edited chapters, so its absence means "never edited" (last updated = published).
  - Redesign pages also contain unrelated `<time>`s (e.g. the author's "member for 2 years"); these must stay unlabeled.
  - Prefer the `unixtime` attribute.
- **Environment gotcha:** python.org Python on macOS needs `Install Certificates.command` (or `SSL_CERT_FILE=/etc/ssl/cert.pem`) before HTTPS works.

### Fetching policy

- Throttle to ~1 request/second, retry with exponential backoff, descriptive User-Agent, respect `robots.txt`/ToS.
- An 800-chapter fiction ≈ 800 requests ≈ 13+ minutes → refresh is a **background job** with progress reporting.
- Keep parsers isolated and covered by saved HTML fixtures so markup changes are cheap to fix.

## 3. Data model

| Table | Key fields |
|---|---|
| **Fiction** | id, rr_fiction_id (unique), title, author, url, cover_url, added_at, last_synced_at, sync_status |
| **Chapter** | id, fiction_id, rr_chapter_id (unique), title, url, order, published_at, last_updated_at, first_seen_at, last_checked_at, status (active / removed / locked) |
| **SyncRun** | id, fiction_id, started_at, finished_at, status, chapters_checked, new_count, updated_count, error |
| **ChapterChange** | chapter_id, sync_run_id, change_type (new / updated / removed / retitled), old_value, new_value, detected_at |

`ChapterChange` provides a per-fiction changelog over time, not just current state.

## 4. Workflows

### Add fiction
1. Validate/normalize URL → extract fiction ID (accept chapter URLs too).
2. Reject duplicates.
3. Fetch fiction page → save metadata + chapter list.
4. Kick off the initial full sync.

### Refresh fiction
1. Fetch fiction page → diff chapter list against the DB:
   - new IDs → insert, flag **new**
   - missing IDs → mark **removed** (don't delete)
   - title/order changes → record
2. Queue fetches for every active, unlocked chapter → parse last-updated date.
3. Date changed → update + log `ChapterChange`.
4. Close the `SyncRun` with counts; stream progress to the UI.
5. Interrupted syncs resume, skipping chapters already checked in that run.

**Later:** "Refresh all", "quick refresh" (chapter list + last N chapters), scheduled refresh.

## 5. Backend API

```
GET    /api/fictions                 list library (with summary stats)
POST   /api/fictions        {url}    add fiction
GET    /api/fictions/:id             fiction + chapters
DELETE /api/fictions/:id             remove from library
POST   /api/fictions/:id/refresh     start sync job → returns job id
GET    /api/jobs/:id                 job progress (or SSE stream)
GET    /api/fictions/:id/changes     changelog history
```

Plus a job queue/worker (in-process for single-user; BullMQ/Celery to scale). One sync per fiction at a time.

## 6. Frontend

- **Library page** — "Add by URL" input; cards/rows with title, author, chapter count, last synced, badge ("3 new, 2 updated"); per-fiction Refresh/Remove; "Refresh all".
- **Fiction detail page** — chapter table: #, title, published, last updated, last checked. Sortable (especially "most recently updated"), highlights new/updated since previous sync, filter "updated only", toggle removed/locked. Live sync progress ("Checking 214 / 800").
- **Changelog view** (optional) — timeline of detected changes across syncs.
- Loading/empty/error states; relative times with exact date on hover.

## 7. Suggested stack

- **TypeScript:** Next.js or Fastify + SQLite (Drizzle/Prisma) + `cheerio` + `p-queue` (+ Playwright if needed for Cloudflare).
- **Python:** FastAPI + SQLite (SQLAlchemy) + BeautifulSoup/selectolax + asyncio worker (+ Playwright).
- Start **single-user, local**. Add auth and per-user libraries only if hosting for others.

## 8. Error handling & edge cases

- Invalid URLs, deleted/hidden fictions, 404 chapters.
- Premium/advance chapters (`subscriptionTiers`), stubbed fictions.
- Cloudflare challenges / rate limiting → back off and surface clearly to the user.
- Parse failures must fail loudly — never silently store null dates.
- Store UTC, display local.

## 9. Testing

- Parser unit tests against saved HTML fixtures (both layouts).
- Diff-logic tests: new, removed, reordered, retitled chapters.
- Sync integration test with a mocked HTTP layer.

## 10. Milestones

1. **Proof of concept** — `poc/rr_poc.py`: given a fiction URL, print every chapter's last-updated date. Validates selectors, Cloudflare handling and rate limits. *(done)*
2. **MVP** — DB + add/refresh API + background sync + basic library and table UI.
3. **Polish** — live progress, new/updated highlighting, sorting/filtering, changelog.
4. **Extras** — scheduled syncs, notifications, multi-user, export.

---

## Milestone 1: running the PoC

Python 3.10+. The core is standard library only; Playwright is needed only for the Cloudflare fallback.

```sh
# One-time setup for the Playwright fallback (project-local venv, gitignored)
python3 -m venv .venv && .venv/bin/pip install -r poc/requirements.txt && .venv/bin/python -m playwright install chromium

# Try a live fetch (first 5 chapters), saving raw HTML for inspection
python3 poc/rr_poc.py https://www.royalroad.com/fiction/21220/mother-of-learning --limit 5 --save-html pages/ -v

# Default --fetcher auto: plain HTTP, switching to off-screen Chromium if Cloudflare blocks it
.venv/bin/python poc/rr_poc.py URL

# Force the browser, or show its window to click through a challenge by hand
.venv/bin/python poc/rr_poc.py URL --fetcher browser --show-browser

# Manual alternative: reuse your own browser's cf_clearance cookie + exact User-Agent
python3 poc/rr_poc.py URL --fetcher http --cookie "cf_clearance=..." --user-agent "Mozilla/5.0 ..."

# Re-parse previously saved pages offline (fiction.html, chapter-<id>.html)
python3 poc/rr_poc.py URL --from-dir pages/ -v

# Tests
python3 -m unittest discover poc
```

**Exit criteria for Milestone 1:** all met (2026-10-09):
- ~~legacy layout returns labelled "Last Edit" dates~~
- ~~redesign layout verified~~ (from a saved chapter page; the browser run happened to be served legacy)
- ~~Cloudflare fallback chosen~~ (Playwright, off-screen headed)
