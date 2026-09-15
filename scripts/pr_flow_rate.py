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

Two further review rounds landed on the SECOND version, and they were
one defect again: it still fetched capped LISTINGS and then inferred
which buckets were complete from the oldest row each happened to return.
That inference cannot hold. The closure search is not ordered by
`closedAt`, so a long-lived PR closed this week can be cut by the cap
while an older returned closure makes the newest bucket look complete --
and the rate and its verdict flip on it.

So the sample was removed instead of being reasoned about:

* **Every bucket is an EXACT COUNT, not a sample.** Each week issues its
  own `search/issues` query and reads `total_count`, which describes the
  whole match set rather than a page. There is no cap to hit, no result
  ordering to depend on, and therefore no completeness to infer -- the
  `--limit` flag and the "horizon" machinery are both gone. MEASURED
  against this repo: `total_count` equals a full listing for a week of
  openings (169) and for the live backlog (75), and still reports 1805
  with `per_page=1`.
* **Openings, closures and the live backlog stay three populations.** A
  PR opened before the window can still be open today, so the backlog is
  its own count and was simply wrong when derived from the openings.
* **Bucket 0 is a complete week**, not a partial one: the windows measure
  backward from `now`, so bucket 0 is the rolling seven-day interval
  ending at this instant -- a full week of elapsed time. The first
  version excluded it as "still running", which silently dropped every
  event from the last seven days and made the published rate a week
  stale, reversing the verdict whenever flow changed quickly.

The cost is `2 * weeks + 1` counting calls rather than three listings.
This is an on-demand measurement a human runs at a terminal, and a
correct number is the entire product.

WHAT THIS DOES NOT MEASURE, stated rather than implied: a REOPEN. The
metric counts creations and closures; reopening a closed PR is neither,
so `net_per_week` is a flow rate and NOT the exact change in backlog.
When PRs are reopened, the queue grows by more than `net` suggests.
Counting reopens needs a per-PR timeline call, which is a different cost
class; the honest move is to say so here rather than let the number be
read as something it is not.

Read-only. Resolves the repo live (never hardcoded) so it works on any
install/fork.

    python3 scripts/pr_flow_rate.py [--weeks N] [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

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


def _count(repo: str, qualifiers: str) -> int:
    """EXACT number of PRs matching *qualifiers*. Not a listing, not a sample.

    This replaced a fetch-rows-then-bucket design, and the reason is the whole
    point of the script. A listing is capped by ``--limit`` and ordered by
    something that is not the timestamp being bucketed — notably the closure
    query, whose results need not be ordered by ``closedAt``. So a long-lived
    PR closed this week could be dropped by the cap while an older returned
    closure made the newest bucket look complete, and the published rate and
    its GROWING/DRAINING verdict could flip on it.

    ``search/issues`` reports ``total_count`` for the whole match set, not for
    the page. MEASURED against this repo: identical to a full listing for a
    week of openings (169) and for the live backlog (75), and it still reports
    1805 with ``per_page=1`` — so it is a true total and is independent of any
    page size. That removes the cap, the ordering assumption, and the
    completeness inference built on top of them in one move: every bucket
    below is an exact count rather than a sample believed to be complete.
    """
    raw = _gh(
        [
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            f"q=repo:{repo} is:pr {qualifiers}",
            "--jq",
            ".total_count",
        ]
    )
    text = (raw or "").strip()
    if not text.isdigit():
        # An unparseable total must never read as zero — zero is a legitimate
        # answer here, so a silent fallback would publish a rate from nothing.
        raise RuntimeError(f"search/issues returned no usable total_count: {text[:200]!r}")
    return int(text)


def _week_window(w: int, now: datetime) -> tuple[str, str]:
    """GitHub search range for bucket *w*, matching the bucket arithmetic exactly.

    Bucket ``w`` is ``[now - (w+1) weeks, now - w weeks)``. GitHub ranges are
    INCLUSIVE at both ends, so the upper bound is pulled back one second —
    otherwise adjacent buckets would both claim an event landing exactly on
    the boundary and the total would exceed the real count.
    """
    start = now - timedelta(weeks=w + 1)
    end = now - timedelta(weeks=w) - timedelta(seconds=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def collect(weeks: int, repo: str, now: datetime) -> dict:
    """Measure opening/closing rates over the last *weeks* complete weeks.

    Each bucket is its OWN server-side query, and each returns an exact count
    rather than a page of rows. That is what retired the completeness
    machinery this function used to carry: a capped listing plus a "horizon"
    inferred from the oldest row it happened to return. The inference was
    unsound in the direction that mattered — the closure search is not ordered
    by `closedAt`, so a long-lived PR closed this week could be cut by the cap
    while an older returned closure made the newest bucket look complete, and
    the verdict could flip on it. There is no cap and no ordering assumption
    left to reason about, so there is no frontier to compute.

    Cost is `2 * weeks + 1` counting calls instead of three listings. This is
    an on-demand measurement a human runs at a terminal, not a hot path, and a
    correct number is the entire product.
    """
    opened: Counter[int] = Counter()
    closed: Counter[int] = Counter()
    for w in range(weeks):
        start, end = _week_window(w, now)
        opened[w] = _count(repo, f"created:{start}..{end}")
        closed[w] = _count(repo, f"closed:{start}..{end}")

    # The backlog is a different population again — PRs open right NOW,
    # whenever they were created. Derived from the openings sample it was
    # simply wrong: a PR opened before the window is open today and invisible
    # there.
    open_now = _count(repo, "is:open")

    # Every bucket is exact, so every bucket is scored. `complete` is kept in
    # the row shape because consumers read it, and it is now a statement
    # rather than an inference. Bucket 0 is a FULL week: `_week_window`
    # measures backward from `now`, so it is the rolling seven-day interval
    # ending at this instant, not a partial week in progress.
    rows = []
    for w in range(weeks):
        rows.append(
            {
                "weeks_ago": w,
                "opened": opened[w],
                "closed": closed[w],
                "net": closed[w] - opened[w],
                "complete": True,
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
        # The oldest instant any bucket covers, derived from the same window
        # helper the buckets use so the reported span cannot drift from the
        # span actually measured.
        "since": _week_window(weeks - 1, now)[0] if weeks else None,
        "counts_are_exact": True,
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
        "currently_open": open_now,
        "verdict": verdict,
        "draining": verdict == "DRAINING",
        "counts_reopens": False,
    }


def render(report: dict) -> str:
    out = [f"PR flow — {report['repo']}", ""]
    out.append(f"{'wks ago':<9}{'opened':>8}{'closed':>8}{'net':>7}")
    for r in report["rows"]:
        out.append(f"{r['weeks_ago']:<9}{r['opened']:>8}{r['closed']:>8}{r['net']:>+7}")
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
        out.append("VERDICT: UNKNOWN — no week in range. Widen --weeks.")
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
    """argparse type: a window bound must be >= 1.

    `--weeks -1` queried the API and reported no weeks at all, printing a
    verdict from an empty sample. A window that cannot represent a span must
    not reach a query.
    """
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weeks", type=_positive, default=10)
    ap.add_argument("--repo", default=None, help="OWNER/REPO (default: resolved live)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        repo = args.repo or _resolve_repo()
        report = collect(args.weeks, repo, datetime.now(UTC))
    except Exception as exc:
        print(f"pr_flow_rate: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
