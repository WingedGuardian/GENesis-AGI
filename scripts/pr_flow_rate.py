#!/usr/bin/env python3
"""PR flow rate — is the open-PR queue draining or growing?

Build sessions open PRs; closing sessions drive them to merge (see the
board design's two-session-type split). That division only works if
closing throughput exceeds opening throughput. This reports whether it
does, because the constraint is a RATE and a rate needs a number:

    closed_per_week > opened_per_week   ->  queue drains
    closed_per_week = opened_per_week   ->  queue holds
    closed_per_week < opened_per_week   ->  queue grows without bound

The third case is not fixable by per-session discipline. It is Little's
Law: with arrivals outpacing departures, WIP grows regardless of how
carefully any individual session behaves. Telling sessions to "close
before you open" relocates the queue upstream; it does not drain it.

This existed as no query at all until 2026-09-02, at which point the
first run showed a 4% leak that had been accumulating for ten weeks
unnoticed. A design that asserts a flow property without shipping its
measurement has no flow property -- hence this script.

THE SAMPLE MUST SUPPORT THE VERDICT
-----------------------------------
Eight review findings landed on the first version, and they were one
defect wearing eight faces: it printed a confident GROWING/DRAINING
verdict from a sample whose completeness it never established. That is
the failure this repo's own core principle names -- "a truncated listing
is not absence" -- committed by the script written to serve it.

So completeness is now COMPUTED, DECLARED, and allowed to withhold the
verdict:

* **Three independent queries, not one.** Openings, closures and the
  live backlog are different populations. A single `--state all` fetch
  is ordered by creation, so hitting its cap drops PRs that were CREATED
  long ago -- exactly the ones that may have CLOSED this week or still
  be open today. Closure and backlog counts were understated by the
  cap, and the verdict could flip on it. Each population is now fetched
  by its own server-side date filter, so one query truncating cannot
  corrupt another's numbers.
* **A truncated query poisons only the buckets it cannot see**, and the
  OLDEST bucket it returns is itself suspect: the cap may have fallen
  partway through that week. It is excluded along with everything older.
* **Bucket 0 is a complete week**, not a partial one. `_weeks_ago`
  measures backward from `now`, so bucket 0 is the rolling seven-day
  interval ending at this instant -- a full week of elapsed time. The
  first version excluded it as "still running", which silently dropped
  every event from the last seven days and made the published rate a
  week stale, reversing the verdict whenever flow changed quickly.

WHAT THIS DOES NOT MEASURE, stated rather than implied: a REOPEN. The
metric counts creations and closures; reopening a closed PR is neither,
so `net_per_week` is a flow rate and NOT the exact change in backlog.
When PRs are reopened, the queue grows by more than `net` suggests.
Counting reopens needs a per-PR timeline call, which is a different cost
class; the honest move is to say so here rather than let the number be
read as something it is not.

Read-only. Resolves the repo live (never hardcoded) so it works on any
install/fork.

    python3 scripts/pr_flow_rate.py [--weeks N] [--limit N] [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

_SECONDS_PER_WEEK = 7 * 24 * 3600

# Bounded on purpose, and this is the sanctioned exception to the repo's
# 7200s floor rather than an oversight: a raw subprocess with NO external
# watchdog, run interactively by a human waiting at a terminal. The failure
# it bounds is a `gh` call that hangs on a network stall -- observed as an
# indefinite wait, not an error -- which would otherwise wedge the command
# with no output and no way to tell it apart from a slow query. 180s is
# ~35x the p100 of a live full-window run measured here (~5s for the
# widest query), so it cannot cut a working measurement short; it only
# ends a call that has stopped making progress.
_GH_TIMEOUT_S = 180


def _gh(args: list[str]) -> str:
    """Run gh, failing LOUDLY: an empty result must never read as 'no PRs'."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["gh", *args], capture_output=True, text=True, timeout=_GH_TIMEOUT_S
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _resolve_repo() -> str:
    return _gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()


def _weeks_ago(iso: str, now: datetime) -> int:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int((now - stamp).total_seconds() // _SECONDS_PER_WEEK)


def _query(
    repo: str, limit: int, search: str, fields: str, state: str = "all"
) -> tuple[list, bool]:
    """One population, server-side filtered. Returns (rows, truncated).

    ``truncated`` is ``len(rows) >= limit`` — the standard "a listing whose
    count equals its limit is a truncated read" test. It is returned rather
    than handled here because each population's truncation invalidates a
    different set of buckets, and collapsing them into one flag is what let a
    creation-ordered cap silently understate closures.
    """
    raw = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--search",
            search,
            "--json",
            fields,
        ]
    )
    rows = json.loads(raw or "[]")
    return rows, len(rows) >= limit


def _bucket(rows: list, key: str, now: datetime) -> Counter[int]:
    counts: Counter[int] = Counter()
    for row in rows:
        stamp = row.get(key)
        if stamp:
            counts[_weeks_ago(stamp, now)] += 1
    return counts


def collect(weeks: int, limit: int, repo: str, now: datetime) -> dict:
    """Measure opening/closing rates over the last *weeks* complete weeks.

    `weeks` bounds the QUERY as well as the report: a server-side date filter
    means the cap has to be hit by events inside the window, not by the repo's
    whole history, which is what made the single-query version truncate on
    ordinary use.
    """
    since = (now - timedelta(weeks=weeks)).date().isoformat()

    opened_rows, opened_truncated = _query(repo, limit, f"created:>={since}", "number,createdAt")
    closed_rows, closed_truncated = _query(
        repo, limit, f"closed:>={since}", "number,closedAt,mergedAt"
    )
    # The backlog is a COUNT of a different population again — PRs open right
    # now, whenever they were created. Derived from the openings sample it was
    # simply wrong: a PR opened before the window is open today and invisible
    # there.
    open_rows, open_truncated = _query(repo, limit, "", "number", state="open")

    opened = _bucket(opened_rows, "createdAt", now)
    closed = _bucket(closed_rows, "closedAt", now)

    # A truncated query is trustworthy only for buckets NEWER than its oldest
    # returned row — and not for that bucket either, because the cap may have
    # fallen partway through that week, so its counts are a floor rather than a
    # total. `horizon` is therefore the oldest bucket that is still COMPLETE.
    def _horizon(counts: Counter[int], truncated: bool) -> int:
        if not truncated or not counts:
            return weeks - 1
        return max(counts) - 1

    horizon = min(
        _horizon(opened, opened_truncated),
        _horizon(closed, closed_truncated),
        weeks - 1,
    )

    # Bucket 0 IS complete: `_weeks_ago` counts backward from `now`, so it is a
    # rolling seven-day interval ending at this instant.
    rows = []
    for w in range(weeks):
        rows.append(
            {
                "weeks_ago": w,
                "opened": opened[w],
                "closed": closed[w],
                "net": closed[w] - opened[w],
                "complete": w <= horizon,
            }
        )

    scored = [r for r in rows if r["complete"]]
    n = len(scored)
    open_rate = sum(r["opened"] for r in scored) / n if n else 0.0
    close_rate = sum(r["closed"] for r in scored) / n if n else 0.0
    net_raw = close_rate - open_rate

    if n == 0:
        verdict = "UNKNOWN"
    elif net_raw > 0:
        verdict = "DRAINING"
    elif net_raw == 0:
        verdict = "FLAT"
    else:
        verdict = "GROWING"

    return {
        "repo": repo,
        "window_weeks": weeks,
        "since": since,
        "samples": {
            "opened": {"rows": len(opened_rows), "truncated": opened_truncated},
            "closed": {"rows": len(closed_rows), "truncated": closed_truncated},
            "open_now": {"rows": len(open_rows), "truncated": open_truncated},
        },
        "complete_weeks_scored": n,
        "rows": rows,
        "open_rate_per_week": round(open_rate, 1),
        "close_rate_per_week": round(close_rate, 1),
        # Rounded for display; `net_raw` is what any arithmetic must use. The
        # rounded value hits 0.0 for a real net below 0.05/wk — reachable with a
        # one-PR difference over 22+ weeks — and dividing the backlog by THAT
        # raised ZeroDivisionError while the verdict still said DRAINING.
        "net_per_week": round(net_raw, 1),
        "net_per_week_raw": net_raw,
        "currently_open": len(open_rows),
        "verdict": verdict,
        "draining": verdict == "DRAINING",
        "counts_reopens": False,
    }


def render(report: dict) -> str:
    out = [f"PR flow — {report['repo']}", ""]
    for name, s in report["samples"].items():
        if s["truncated"]:
            out.append(
                f"NOTE: the '{name}' query hit its {s['rows']}-row cap. Its oldest "
                "week (and everything older) is a floor, not a total, and is "
                "excluded from the rate. Raise --limit to widen."
            )
    if any(s["truncated"] for s in report["samples"].values()):
        out.append("")
    out.append(f"{'wks ago':<9}{'opened':>8}{'closed':>8}{'net':>7}")
    for r in report["rows"]:
        mark = "" if r["complete"] else "  (incomplete — excluded)"
        out.append(f"{r['weeks_ago']:<9}{r['opened']:>8}{r['closed']:>8}{r['net']:>+7}{mark}")
    n = report["complete_weeks_scored"]
    out += [
        "",
        f"over {n} complete week(s) since {report['since']}:",
        f"  opened  {report['open_rate_per_week']}/wk",
        f"  closed  {report['close_rate_per_week']}/wk",
        f"  net     {report['net_per_week']:+}/wk",
        f"  open now {report['currently_open']}",
        "",
    ]
    verdict = report["verdict"]
    if verdict == "UNKNOWN":
        out.append(
            "VERDICT: UNKNOWN — no complete week in range. Widen --weeks, or "
            "raise --limit if a query truncated above."
        )
    elif verdict == "DRAINING":
        weeks_left = report["currently_open"] / report["net_per_week_raw"]
        out.append(f"VERDICT: DRAINING — queue clears in ~{weeks_left:.0f} weeks at this rate.")
    elif verdict == "FLAT":
        out.append(
            "VERDICT: FLAT — closing exactly matches opening, so the queue holds "
            "at its current size. It does not grow, and it does not clear."
        )
    else:
        out.append(
            "VERDICT: GROWING — closing is not keeping up, so the queue grows "
            "without bound. Per-session discipline cannot fix this; closing "
            "capacity is the control variable."
        )
    out.append(
        "(net counts creations and closures, NOT reopens — a reopened PR "
        "rejoins the queue without being counted as an arrival.)"
    )
    return "\n".join(out)


def _positive(value: str) -> int:
    """argparse type: a sampling bound must be >= 1.

    `--limit 0` returned an empty fetch that read as "no PRs", scored every
    week as complete-and-zero, and printed the factual verdict GROWING;
    `--weeks -1` queried the API and reported no complete weeks. Neither can
    represent a sample, so neither reaches a query.
    """
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weeks", type=_positive, default=10)
    ap.add_argument("--limit", type=_positive, default=800, help="PRs per query")
    ap.add_argument("--repo", default=None, help="OWNER/REPO (default: resolved live)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        repo = args.repo or _resolve_repo()
        report = collect(args.weeks, args.limit, repo, datetime.now(UTC))
    except Exception as exc:
        print(f"pr_flow_rate: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
