# RR-Changelog — Project Outline

A web app for viewing the **last-updated date of every chapter** in a RoyalRoad fiction without opening each chapter page. Users add fictions to a library by URL and refresh them to re-check every chapter and pick up new ones.

---

## 1. Architecture

Browsers cannot fetch `royalroad.com` directly (CORS), so scraping must run outside the page:

- **Backend server (chosen)** — the server fetches/parses RoyalRoad; the frontend only talks to our API.
- *Alternative:* a browser extension, which avoids CORS and uses the user's own session (and their Cloudflare clearance — see §2).

## 2. Scraping layer

### Known findings (2026-10-09)

- **Cloudflare is inconsistent.** `curl` (and WebFetch) got a `403` "Just a moment..." challenge, while Python `urllib` from the same machine got through. It likely depends on the client's TLS fingerprint, so it can change without warning. The PoC detects the challenge page and suggests fallbacks:
  - reuse a real browser's `cf_clearance` cookie + matching User-Agent,
  - a headless browser (Playwright) as an escalation path,
  - running in the user's browser (extension / userscript).
  This is still the **biggest operational risk**. Detect and surface it, never mistake it for "no chapters".
- **Two RoyalRoad layouts are live.** A redesign has been in testing since Sept 2026 alongside the legacy UI. Parsers must handle both.
- **Fiction page data sources** (per FanFicFare's maintained adapter):
  - `window.chapters = [...]` JSON in a `<script>`: `id, volumeId, title, slug, date (ISO, published), order, visible, subscriptionTiers, isUnlocked, url`. Present in both layouts.
  - `<script type="application/ld+json">` (redesign): `name, author.name, author.url, description, datePublished, dateModified`.
  - Legacy fallback: `table#chapters tbody tr` rows with a chapter link and `<time unixtime="...">`.
  - Chapters with non-empty `subscriptionTiers` are premium/advance and not publicly readable.
- **Chapter page dates (verified on the legacy layout):**
  ```html
  <i class="fa fa-calendar" title="Published"></i> <time unixtime="1540762483" datetime="2018-10-28T21:34:43.0000000Z">…</time>
  <i class="fa fa-edit" title="Last Edit"></i> <time unixtime="1638566602" datetime="2021-12-03T21:23:22.0000000Z">…</time>
  ```
  The "Last Edit" entry appears only on edited chapters, so its absence means "never edited" (last updated = published). Prefer the `unixtime` attribute. The redesign's chapter markup is still unverified. The PoC classifies every `<time>` by its label and reports which source it used, so a layout change shows up instead of failing silently.
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

1. **Proof of concept** — `poc/rr_poc.py`: given a fiction URL, print every chapter's last-updated date. Validates selectors, Cloudflare handling and rate limits. *(working on the legacy layout; redesign still to check)*
2. **MVP** — DB + add/refresh API + background sync + basic library and table UI.
3. **Polish** — live progress, new/updated highlighting, sorting/filtering, changelog.
4. **Extras** — scheduled syncs, notifications, multi-user, export.

---

## Milestone 1: running the PoC

Python 3.10+, standard library only.

```sh
# Try a live fetch (first 5 chapters), saving raw HTML for inspection
python3 poc/rr_poc.py https://www.royalroad.com/fiction/21220/mother-of-learning --limit 5 --save-html pages/ -v

# If Cloudflare blocks it: open the fiction in your browser, then copy the
# cf_clearance cookie and your browser's exact User-Agent
python3 poc/rr_poc.py URL --cookie "cf_clearance=..." --user-agent "Mozilla/5.0 ..."

# Re-parse previously saved pages offline (fiction.html, chapter-<id>.html)
python3 poc/rr_poc.py URL --from-dir pages/ -v

# Tests
python3 -m unittest discover poc
```

**Exit criteria for Milestone 1:** ~~legacy layout returns labelled "Last Edit" dates~~ (done), redesign layout verified, and the Cloudflare fallback strategy for Milestone 2 chosen.
