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
  - In the web app (later that day) plain HTTP was blocked on every sync, so the browser path is the normal path, not a rare fallback.
  - Left alone, RoyalRoad's ad scripts pegged the browser's CPU, and one refresh took 37 min instead of ~5. The fetcher now blocks every request that isn't to `royalroad.com`/`cloudflare.com` (and images/fonts), and parks the tab on `about:blank` between fetches. That gives ≈1.4 s per fetch, ≈2.8 s per chapter with the 1 s throttle (109 chapters in 5m08s).
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
- **Environment gotcha:** python.org Python on macOS has no CA certificates by default. The scraper uses `certifi` when it's installed (it is in `.venv`); otherwise run `Install Certificates.command` or set `SSL_CERT_FILE=/etc/ssl/cert.pem`.

### Fetching policy

- Throttle to ~1 request/second, retry with exponential backoff, descriptive User-Agent, respect `robots.txt`/ToS.
- An 800-chapter fiction ≈ 800 requests ≈ 13+ minutes → refresh is a **background job** with progress reporting.
- Keep parsers isolated and covered by saved HTML fixtures so markup changes are cheap to fix.

## 3. Data model

As implemented in `rrchangelog/db.py` (SQLite, UTC ISO-8601 timestamps):

| Table | Key fields |
|---|---|
| **fictions** | id, rr_fiction_id (unique), title, author, url, cover_url, added_at, last_synced_at |
| **chapters** | id, fiction_id, rr_chapter_id (unique), title, url, position, published_at, edited_at (NULL = never edited), status (active / locked / removed), first_seen_at, last_checked_at, check_error |
| **sync_runs** | id, fiction_id, status (queued / running / done / failed / interrupted), queued/started/finished_at, chapters_total, chapters_checked, new/edited/removed/error counts, error |
| **chapter_changes** | chapter_id, sync_run_id, change_type (new / edited / removed / restored / retitled / unlocked), old_value, new_value, detected_at |

- "Last updated" = `edited_at` if set, else `published_at`.
- `chapter_changes` gives a per-fiction changelog over time, not just the current state.
- A fiction's **first** completed sync is a baseline: it stores chapters but logs no changes.

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
5. *(Not yet built)* Interrupted syncs resume, skipping chapters already checked in that run. For now a server restart marks running syncs `interrupted`, and queued ones are picked up again.

"Refresh all" is built. **Later:** "quick refresh" (chapter list + last N chapters), scheduled refresh.

## 5. Backend API

```
GET    /api/fictions                 list library (with summary stats)
POST   /api/fictions        {url}    add fiction
GET    /api/fictions/:id             fiction + chapters
DELETE /api/fictions/:id             remove from library
POST   /api/fictions/:id/refresh     start sync job → returns job id
GET    /api/jobs/:id                 job progress (polled by the UI)
GET    /api/fictions/:id/changes     changelog history
POST   /api/refresh-all              queue a sync for every fiction not already syncing
```

The worker (`rrchangelog/worker.py`) is one in-process thread that runs queued syncs in order. One thread means one throttle and one browser for every sync, and Playwright objects are thread-bound anyway. Requesting a refresh while one is queued or running returns 409.

## 6. Frontend

- **Library page** — "Add by URL" input; cards/rows with title, author, chapter count, last synced, badge ("3 new, 2 updated"); per-fiction Refresh/Remove; "Refresh all".
- **Fiction detail page** — chapter table: #, title, published, last updated, last checked. Sortable (especially "most recently updated"), highlights new/updated since previous sync, filter "updated only", toggle removed/locked. Live sync progress ("Checking 214 / 800").
- **Changelog view** (optional) — timeline of detected changes across syncs.
- Loading/empty/error states; relative times with exact date on hover.

## 7. Stack (chosen)

- **FastAPI + SQLite** (stdlib `sqlite3`, no ORM) + the stdlib-`HTMLParser` scraper from Milestone 1 + Playwright for Cloudflare.
- Frontend: plain HTML/CSS/JS served by FastAPI. There's no build step.
- **Single-user, local.** Add auth and per-user libraries only if hosting for others.

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
2. **MVP** — DB + add/refresh API + background sync + basic library and table UI. *(done, see "Running the app")*
3. **Polish** — live progress, new/updated highlighting, sorting/filtering, changelog.
4. **Extras** — scheduled syncs, notifications, multi-user, export.

---

## Running the app

```sh
# One-time setup (project-local venv, gitignored)
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium

# Start the web app → http://127.0.0.1:8000
.venv/bin/python -m rrchangelog
```

Settings are environment variables (see `rrchangelog/api.py`):

| Variable | Meaning |
|---|---|
| `RR_DB_PATH` | SQLite file (default `data/rr-changelog.sqlite3`) |
| `RR_FETCHER` | `auto` (default) / `http` / `browser` |
| `RR_DELAY` | Seconds between RoyalRoad requests (default `1.0`) |
| `RR_SHOW_BROWSER=1` | Show the Chromium window, e.g. to solve a Cloudflare challenge |
| `RR_LOG_LEVEL=DEBUG` | Log every fetch's duration |

### Layout

```
rrchangelog/scraper.py   fetch + parse RoyalRoad pages (HTTP → Playwright fallback)
rrchangelog/db.py        schema + connection helpers
rrchangelog/sync.py      one sync run: chapter-list diff, per-chapter date check, change log
rrchangelog/worker.py    background thread running queued syncs
rrchangelog/api.py       FastAPI routes + static frontend
rrchangelog/static/      library page, fiction page
poc/rr_poc.py            Milestone 1 CLI (now a thin wrapper over scraper.py)
tests/                   unittest suite (scraper, sync, API); all offline
```

### PoC CLI and tests

```sh
.venv/bin/python poc/rr_poc.py https://www.royalroad.com/fiction/21220/mother-of-learning --limit 5 -v
.venv/bin/python poc/rr_poc.py URL --fetcher browser --show-browser   # click through a challenge by hand
.venv/bin/python poc/rr_poc.py URL --from-dir pages/ -v               # re-parse saved pages offline

.venv/bin/python -m unittest discover -s tests -t .
```
