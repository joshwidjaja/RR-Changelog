"""
RR-Changelog CLI.

    python3 cli.py add https://www.royalroad.com/fiction/141517/cursed-relic-collector
    python3 cli.py refresh 141517 --recent 20
    python3 cli.py list 141517 --sort updated
    python3 cli.py unread 141517
    python3 cli.py history 141517
    python3 cli.py read 141517 --through 110

`refresh` is the expensive command: one HTTP request per chapter, ~1.8 s each,
with no way to make it cheaper (RoyalRoad sends no cache validators). So the
default is a partial sweep of the most recently touched chapters -- observed
edits cluster shortly after publication -- and a full sweep is opt-in.
"""

import argparse
from collections import Counter
import sys
from datetime import datetime, timezone

import db as D
import rr
from fetch import Fetcher, FetchError

DEFAULT_RECENT = 25


def fmt(ts, width=16):
    """ISO string -> 'YYYY-MM-DD HH:MM', or a dash for the meaningful NULL."""
    if not ts:
        return "-".ljust(width)
    return ts.replace("T", " ")[:width].ljust(width)


def resolve(conn, ident):
    """Accept a RoyalRoad id or any URL containing one."""
    m = rr.FICTION_URL_RE.search(str(ident))
    rr_id = int(m.group(1)) if m else int(ident)
    row = conn.execute("SELECT * FROM fictions WHERE rr_id = ?", (rr_id,)).fetchone()
    if row is None:
        sys.exit(f"fiction {rr_id} not saved yet -- run: add <url>")
    return row


# --------------------------------------------------------------------------

def cmd_add(conn, args):
    with Fetcher(min_interval=args.delay) as f:
        html = f.get(args.url)
    data = rr.parse_toc(html, url=args.url)
    meta, chapters = data["fiction"], data["chapters"]

    fid = D.upsert_fiction(conn, rr_id=meta["rr_id"], title=meta["title"],
                           url=meta["url"], author=meta["author"])
    counts = {}
    for ch in chapters:
        # The ToC gives publication dates only -- no edit info exists here, which
        # is the entire reason `refresh` has to visit each chapter page. Seed the
        # rows now so `list` works immediately, and leave updated_at NULL until a
        # chapter page is actually read.
        r = D.upsert_chapter(conn, fid, ch)
        counts[r] = counts.get(r, 0) + 1

    print(f"{meta['title']} by {meta['author']}  (rr_id {meta['rr_id']})")
    print(f"  {len(chapters)} chapters in the index: "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    skipped = len(chapters) - len(rr.fetchable(chapters))
    if skipped:
        print(f"  {skipped} not fetchable (hidden or subscriber-locked)")
    print(f"\n  next: refresh {meta['rr_id']}   <- reads each chapter page for edit dates")


def cmd_refresh(conn, args, fetcher=None):
    """
    Visit chapter pages and record edits.

    `fetcher` is injectable so the end-to-end test can run against saved
    fixtures instead of the network.
    """
    fic = resolve(conn, args.fiction)
    rows = D.stale_first(conn, fic["id"], limit=None if args.full else (args.recent or DEFAULT_RECENT))
    targets = [dict(r) for r in rows]

    scope = "all" if args.full else f"{len(targets)} most recently touched"
    est = len(targets) * (args.delay + 0.8)
    print(f"{fic['title']}: checking {scope} chapters (~{est/60:.1f} min)\n")

    own = fetcher is None
    f = fetcher or Fetcher(min_interval=args.delay)
    changed, tally, errors = [], Counter(), 0
    try:
        for i, row in enumerate(targets, 1):
            try:
                page = rr.parse_chapter(f.get(row["url"]))
            except (FetchError, rr.ParseError) as e:
                errors += 1
                print(f"  [{i}/{len(targets)}] !! {row['title'][:44]}: {e}")
                continue

            page["position"] = row["position"]
            result = D.upsert_chapter(conn, fic["id"], page)
            tally[result] += 1
            if result in ("edited", "new"):
                changed.append((row["position"], row["title"], result))
                print(f"  [{i}/{len(targets)}] {result.upper():9} {row['title'][:50]}")
            elif args.verbose:
                print(f"  [{i}/{len(targets)}] {result:9} {row['title'][:50]}")
    except KeyboardInterrupt:
        # Each chapter commits on its own, so an interrupted refresh keeps
        # everything already fetched. Resuming just re-runs the command.
        print("\n  interrupted -- progress so far is saved.")
    finally:
        if own:
            f.close()

    D.mark_refreshed(conn, fic["id"])

    if tally["baseline"]:
        # First time these pages have been read. Report what was learned about
        # the past rather than pretending it all just happened.
        already = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE fiction_id = ? AND updated_at IS NOT NULL",
            (fic["id"],)).fetchone()[0]
        print(f"\n  baseline established for {tally['baseline']} chapter(s); "
              f"{already} had been edited before tracking started")
    print(f"  {len(changed)} changed, {tally['unchanged']} unchanged"
          + (f", {tally['retitled']} retitled" if tally["retitled"] else "")
          + (f", {errors} errors" if errors else ""))

    # Sanity check born from a real bug. RoyalRoad injects an anti-plagiarism
    # sentence behind a per-request random class; before that was normalised
    # away, every chapter's hash differed on every fetch and the tool reported
    # all of them as edited. If that fraction is ever that high again, the far
    # likelier explanation is that normalisation broke -- not that an author
    # revised their entire back catalogue between two refreshes.
    checked = sum(tally.values())
    if checked >= 10 and len(changed) > checked * 0.5 and not tally["baseline"]:
        print(f"\n  ! {len(changed)} of {checked} chapters reported as edited at once.")
        print("    That usually means content normalisation is broken (RoyalRoad")
        print("    changed how it hides injected text), not a real mass edit.")
        print("    Re-check rr.strip_hidden() against a freshly saved chapter page.")

    return changed


def cmd_list(conn, args):
    fic = resolve(conn, args.fiction)
    rows = D.list_chapters(conn, fic["id"], order=args.sort)
    print(f"{fic['title']}  ({len(rows)} chapters, sorted by {args.sort})\n")
    print(f"  {'#':>4}  {'published':16}  {'last edit':16}  title")
    print(f"  {'-'*4}  {'-'*16}  {'-'*16}  {'-'*40}")
    for r in rows[: args.limit] if args.limit else rows:
        mark = "*" if r["updated_at"] else " "
        print(f"  {r['position']:>4}{mark} {fmt(r['published_at'])}  {fmt(r['updated_at'])}  {r['title'][:44]}")
    edited = sum(1 for r in rows if r["updated_at"])
    print(f"\n  * = edited since publication ({edited} of {len(rows)})")


def cmd_unread(conn, args):
    fic = resolve(conn, args.fiction)
    rows = D.needs_reread(conn, fic["id"])
    if not rows:
        print("nothing to re-read.")
        return
    print(f"{fic['title']}: {len(rows)} chapter(s) edited since you read them\n")
    for r in rows:
        print(f"  {r['position']:>4}  {fmt(r['updated_at'])}  {r['title'][:50]}")
        print(f"        {r['url']}")


def cmd_history(conn, args):
    fic = resolve(conn, args.fiction)
    rows = D.revision_history(conn, fic["id"])
    if not rows:
        print("no revisions observed yet -- run refresh at least twice.")
        return
    print(f"{fic['title']}: {len(rows)} observed revision(s)\n")
    for r in rows[: args.limit] if args.limit else rows:
        delta = f"{r['word_delta']:+d} words" if r["word_delta"] is not None else "size unknown"
        was = "first edit" if r["old_updated_at"] is None else fmt(r["old_updated_at"], 16).strip()
        print(f"  {fmt(r['observed_at'])}  ch{r['position']:<4} {r['title'][:36]:38} {delta:>14}  (was: {was})")


def cmd_read(conn, args):
    fic = resolve(conn, args.fiction)
    D.mark_read(conn, fic["id"], up_to_position=args.through)
    where = f"through chapter {args.through}" if args.through else "all chapters"
    print(f"marked {where} as read, as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")


# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="rr-changelog", description=__doc__.split("\n")[1])
    p.add_argument("--db", default="rrchangelog.db")
    p.add_argument("--delay", type=float, default=1.0,
                   help="minimum seconds between requests (default 1.0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="save a fiction and seed its chapter index")
    a.add_argument("url")

    r = sub.add_parser("refresh", help="read chapter pages and record edits")
    r.add_argument("fiction")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, help=f"check N most recently touched (default {DEFAULT_RECENT})")
    g.add_argument("--full", action="store_true", help="check every chapter (slow)")
    r.add_argument("-v", "--verbose", action="store_true")

    l = sub.add_parser("list", help="show the chapter table")
    l.add_argument("fiction")
    l.add_argument("--sort", choices=["position", "updated", "published"], default="position")
    l.add_argument("--limit", type=int)

    u = sub.add_parser("unread", help="chapters edited since you read them")
    u.add_argument("fiction")

    h = sub.add_parser("history", help="the changelog")
    h.add_argument("fiction")
    h.add_argument("--limit", type=int)

    rd = sub.add_parser("read", help="mark chapters read")
    rd.add_argument("fiction")
    rd.add_argument("--through", type=int, help="mark read up to this position")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = D.connect(args.db)
    D.init(conn)
    try:
        {"add": cmd_add, "refresh": cmd_refresh, "list": cmd_list,
         "unread": cmd_unread, "history": cmd_history, "read": cmd_read}[args.cmd](conn, args)
    except (FetchError, rr.ParseError) as e:
        sys.exit(f"error: {e}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
