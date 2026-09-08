# RR-Changelog

Tracks when RoyalRoad chapters were **last edited**, so you can tell what's worth
re-reading without opening every chapter by hand.

RoyalRoad publishes a `Last Edit` timestamp on each chapter page, but its chapter
index shows only publication dates. On a 100+ chapter fiction there is no way to
answer "what changed since I last read this?" short of opening every chapter. This
tool collects those timestamps into one table and keeps a changelog of edits it
observes over time.

```
$ rr-changelog list 141517 --sort updated

Cursed Relic Collector  (117 chapters, sorted by updated)

     #  published         last edit         title
  ----  ----------------  ----------------  --------------------------------
   109* 2026-08-09 16:56  2026-08-10 03:37  Chapter 109 - A Day in Cindralet
   110  2026-08-13 16:51  -                 Chapter 110 - Lightning Lodestones
     1* 2025-11-26 15:13  2026-07-28 14:16  Chapter 1 - To Protect a Relic

  * = edited since publication (2 of 117)
```

## Install

```bash
pip install httpx beautifulsoup4 lxml
```

Python **3.11+**. Earlier versions raise on RoyalRoad's seven-digit fractional
seconds in `datetime.fromisoformat` — the parser trims them, but 3.11 is the
tested floor.

## Use

```bash
python3 cli.py add https://www.royalroad.com/fiction/141517/cursed-relic-collector
python3 cli.py refresh 141517              # 25 most recently touched chapters
python3 cli.py refresh 141517 --full       # every chapter (slow, see below)
python3 cli.py list 141517 --sort updated
python3 cli.py unread 141517               # edited since you last read them
python3 cli.py history 141517              # the changelog
python3 cli.py read 141517 --through 110   # mark progress
```

`add` seeds the chapter list from one cheap request. `refresh` is the expensive
one: it must open each chapter page, because the edit timestamp exists nowhere
else.

## Design notes

**Why refresh costs what it does.** RoyalRoad serves chapter pages with
`Cache-Control: no-cache, no-store` and no `ETag` or `Last-Modified`, so
conditional requests are impossible — there is no cheap "did this change?" probe.
Measured at ~0.8 s latency and ~105 KB per page, at a 1 req/s floor:

| Chapters | Full refresh | Downloaded |
|---|---|---|
| 50 | ~1.5 min | ~5 MB |
| 300 | ~9 min | ~31 MB |

So the default is a **partial sweep** of recently-touched chapters, and `--full`
is opt-in. Observed edits cluster shortly after publication (one sampled chapter
was edited 11 hours later), though not exclusively — another was edited 8 months
on — which is why periodic full sweeps still matter.

**Why the timestamp and not a content hash.** An earlier design fingerprinted
chapter text and diffed it across runs. Reading the actual timestamp is strictly
better: it reports edits that happened *before* you started tracking, where
fingerprinting starts empty and is useless on first run. Hashes are still stored,
but as a secondary signal — edit magnitude, and catching a trivial save that
bumps the clock without changing anything.

**`updated_at IS NULL` is meaningful.** RoyalRoad omits the `Last Edit` row
entirely for a chapter that has never been edited, so NULL records "never
edited", not "unknown". Queries sort on `COALESCE(updated_at, published_at)` so
unedited chapters land in the right place, while the column itself stays raw.

**Selectors key on labels, not positions.** On a chapter page the
`<i title="Last Edit">` icon labels its sibling `<time>`. Because an unedited
chapter has one fewer `<li>`, a positional selector like `li:nth-child(2) time`
reads the wrong row or nothing at all. Every site-specific selector lives in
`rr.py` so a markup change is one file to fix.

**Hidden injected content is stripped before hashing.** RoyalRoad inserts an
anti-plagiarism sentence into every chapter body, concealed behind a class name
that is randomised per request — and the sentence itself rotates too. Hashing the
raw body therefore produces a different digest on every fetch, which made the
first live run report all 117 chapters as edited. `rr.strip_hidden()` reads the
page's own CSS for whatever it declares `display: none` and drops those nodes,
rather than pattern-matching RoyalRoad's warning phrases — a list that would rot
the moment they add a new one. `refresh` also warns if more than half of the
chapters checked report as edited at once, since that is far more likely to mean
normalisation broke than that an author revised their entire back catalogue.

**Baseline is not a revision.** The first refresh fills in edit dates for every
chapter at once. Treating that as change would write one bogus revision per
chapter and bury the real changelog, so it's recorded as a baseline instead.

## Ethics and rate limiting

RoyalRoad's `robots.txt` disallows two paths, neither of them chapter pages, and
separately blocks AI training crawlers wholesale (GPTBot, CCBot, ClaudeBot, and
others). This tool is deliberately not that:

- **Identifies honestly** in its User-Agent, and does not impersonate a browser.
  The reason it's acceptable is that it isn't what RoyalRoad is objecting to;
  disguising it would make that false.
- **Stores fingerprints, never chapter text.** No prose is retained or
  redistributed — only timestamps, word counts, and a truncated hash. It is not
  a reader and cannot reconstruct a chapter.
- **Rate limits** to 1 request/second by default, with backoff and `Retry-After`
  handling.
- **Personal use, low volume.** Not intended to be run as a hosted service.

## Tests

```bash
python3 test_db.py     # 29 - storage, NULL semantics, upsert, revisions
python3 test_rr.py     # 41 - parsers, hidden-injection handling, fixtures
python3 test_e2e.py    # 18 - add -> refresh -> detect -> unread -> history
```

Tests run against saved fixtures in `fixtures/`, never the live site. The two
chapter fixtures cover both markup shapes — one edited, one never edited — which
is the distinction most likely to break a parser.

## Layout

```
db.py      SQLite storage: schema, upserts, queries
rr.py      RoyalRoad adapter — all site-specific selectors live here
fetch.py   rate-limited HTTP client
cli.py     commands
fixtures/  saved HTML for tests
```
