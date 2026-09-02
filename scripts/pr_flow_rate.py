#!/usr/bin/env python3
"""PR flow rate — is the open-PR queue draining or growing?

Build sessions open PRs; closing sessions drive them to merge (see the
board design's two-session-type split). That division only works if
closing throughput exceeds opening throughput. This reports whether it
does, because the constraint is a RATE and a rate needs a number:

    closed_per_week > opened_per_week   ->  queue drains
    closed_per_week < opened_per_week   ->  queue grows without bound

The second case is not fixable by per-session discipline. It is Little's
Law: with arrivals outpacing departures, WIP grows regardless of how
carefully any individual session behaves. Telling sessions to "close
before you open" relocates the queue upstream; it does not drain it.

This existed as no query at all until 2026-09-02, at which point the
first run showed a 4% leak that had been accumulating for ten weeks
unnoticed. A design that asserts a flow property without shipping its
measurement has no flow property -- hence this script.

TRUNCATION IS LOUD. `gh pr list` caps results, and a capped read looks
exactly like a quiet period: weeks fall to zero because the page ended,
not because nothing happened. When the sample hits the cap, the affected
weeks are marked INCOMPLETE and excluded from the rate rather than
silently averaged in.

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
from datetime import UTC, datetime

_SECONDS_PER_WEEK = 7 * 24 * 3600


def _gh(args: list[str]) -> str:
    """Run gh, failing LOUDLY: an empty result must never read as 'no PRs'."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _resolve_repo() -> str:
    return _gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()


def _weeks_ago(iso: str, now: datetime) -> int:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int((now - stamp).total_seconds() // _SECONDS_PER_WEEK)


def collect(weeks: int, limit: int, repo: str, now: datetime) -> dict:
    raw = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,createdAt,mergedAt,closedAt,state",
        ]
    )
    prs = json.loads(raw or "[]")
    truncated = len(prs) >= limit

    opened: Counter[int] = Counter()
    closed: Counter[int] = Counter()
    for pr in prs:
        opened[_weeks_ago(pr["createdAt"], now)] += 1
        ended = pr.get("mergedAt") or pr.get("closedAt")
        if ended:
            closed[_weeks_ago(ended, now)] += 1

    # A truncated sample is complete only back to its oldest CREATED PR;
    # anything older is missing rows, not quiet.
    horizon = max(opened) if (truncated and opened) else weeks

    # Week 0 is partial by definition (the current week is still running),
    # so it is reported but excluded from the rate.
    rows = []
    for w in range(min(weeks, horizon + 1)):
        rows.append(
            {
                "weeks_ago": w,
                "opened": opened[w],
                "closed": closed[w],
                "net": closed[w] - opened[w],
                "complete": 0 < w <= horizon,
            }
        )

    scored = [r for r in rows if r["complete"]]
    n = len(scored) or 1
    open_rate = sum(r["opened"] for r in scored) / n
    close_rate = sum(r["closed"] for r in scored) / n
    currently_open = sum(1 for p in prs if p["state"] == "OPEN")

    return {
        "repo": repo,
        "sample_size": len(prs),
        "sample_truncated": truncated,
        "complete_weeks_scored": len(scored),
        "rows": rows,
        "open_rate_per_week": round(open_rate, 1),
        "close_rate_per_week": round(close_rate, 1),
        "net_per_week": round(close_rate - open_rate, 1),
        "currently_open": currently_open,
        "draining": close_rate > open_rate,
    }


def render(report: dict) -> str:
    out = [f"PR flow — {report['repo']}", ""]
    if report["sample_truncated"]:
        out.append(
            f"NOTE: sample hit the {report['sample_size']}-PR cap. Weeks beyond "
            "the oldest sampled PR are omitted, not zero. Raise --limit to widen."
        )
        out.append("")
    out.append(f"{'wks ago':<9}{'opened':>8}{'closed':>8}{'net':>7}")
    for r in report["rows"]:
        mark = "" if r["complete"] else "  (partial — excluded)"
        out.append(f"{r['weeks_ago']:<9}{r['opened']:>8}{r['closed']:>8}{r['net']:>+7}{mark}")
    n = report["complete_weeks_scored"]
    out += [
        "",
        f"over {n} complete week(s):",
        f"  opened  {report['open_rate_per_week']}/wk",
        f"  closed  {report['close_rate_per_week']}/wk",
        f"  net     {report['net_per_week']:+}/wk",
        f"  open now {report['currently_open']}",
        "",
    ]
    if n == 0:
        out.append("VERDICT: no complete week in range — widen --weeks or --limit.")
    elif report["draining"]:
        weeks_left = report["currently_open"] / report["net_per_week"]
        out.append(f"VERDICT: DRAINING — queue clears in ~{weeks_left:.0f} weeks at this rate.")
    else:
        out.append(
            "VERDICT: GROWING — closing is not keeping up, so the queue grows "
            "without bound. Per-session discipline cannot fix this; closing "
            "capacity is the control variable."
        )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weeks", type=int, default=10)
    ap.add_argument("--limit", type=int, default=800, help="PRs to sample")
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
