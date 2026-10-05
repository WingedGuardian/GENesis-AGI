#!/usr/bin/env python3
"""Re-derive the PR size bands from merged history, using ``pr_shape``.

The bands in ``pr_shape.py`` (``SHAPE_AT`` / ``OVERRIDE_AT``) came from a study
whose counting method was not recorded. This script re-runs it with the
counter that applies them, so the derivation can be repeated:

1. Take the most recently created merged PRs (``--limit``, default 400).
2. Size each one by its squash commit on the default branch,
   ``git diff -M <merge>^1 <merge>``, counted with ``pr_shape.count_diff``.
3. Read each one's review rounds with ``scripts/review_budget.py``. A PR whose
   budget status is not ``ok`` is excluded, and the exclusion is counted.
4. Report the median rounds per counted-size bucket, a Spearman rank
   correlation, and the thresholds this rule implies:
   - shape: the lower edge of the first bucket (n >= 5) whose median reaches
     3 rounds, the point where the commit gate's round-2 stop has fired;
   - override: the first such bucket whose median reaches 4, the terminal round.

Rounds before ``ROUND_RULE_CUTOVER_ISO`` follow the legacy rule (every head the
primary reviewer saw, clean ones included), so results are reported for each
era as well as overall. A plain count (added plus removed lines over the same
files) is reported beside the counted one for comparison.

Run from a checkout whose default branch is fetched; it needs ``gh`` auth.
Writes ``rows.jsonl`` (resumable) and ``report.json`` to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))
import pr_shape  # noqa: E402

BUCKETS = [
    (0, 50),
    (50, 200),
    (200, 400),
    (400, 600),
    (600, 800),
    (800, 1000),
    (1000, 1500),
    (1500, None),
]
MIN_BUCKET_N = 5


def plain_count(diff: str, excluded: dict) -> int:
    """Added plus removed non-blank lines, over the files ``count_diff`` counted."""
    n, path, old_path = 0, None, None
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else None
            continue
        if line.startswith("+++ "):
            # A deleted file's new side is /dev/null; count it under its old path.
            path = line[6:] if line.startswith("+++ b/") else old_path
            continue
        if path is None or path in excluded:
            continue
        if line.startswith(("+", "-")) and line[1:].strip():
            n += 1
    return n


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation with average ranks for ties; 0.0 when undefined."""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    if len(xs) < 2:
        return 0.0
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def bucket_table(rows: list[dict], key: str) -> list[dict]:
    """Median and 75th-percentile rounds per size bucket of ``rows[key]``."""
    out = []
    for lo, hi in BUCKETS:
        r = sorted(x["rounds"] for x in rows if x[key] >= lo and (hi is None or x[key] < hi))
        out.append(
            {
                "bucket": f"{lo}-{hi if hi is not None else '+'}",
                "lo": lo,
                "n": len(r),
                "median": statistics.median(r) if r else None,
                "p75": r[int(0.75 * (len(r) - 1))] if r else None,
            }
        )
    return out


def implied_thresholds(table: list[dict]) -> tuple[int | None, int | None]:
    """(shape, override): first bucket edges whose median reaches 3 and 4 rounds."""
    shape = override = None
    for row in table:
        if row["median"] is None or row["n"] < MIN_BUCKET_N:
            continue
        if shape is None and row["median"] >= 3:
            shape = row["lo"]
        if override is None and row["median"] >= 4:
            override = row["lo"]
    return shape, override


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, cwd=_ROOT).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="OWNER/REPO (default: the checkout's GitHub repo)")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    args = ap.parse_args(argv)
    repo = (
        args.repo
        or _sh("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    )
    args.out.mkdir(parents=True, exist_ok=True)
    import review_budget  # noqa: PLC0415

    cutover = review_budget.ROUND_RULE_CUTOVER_ISO
    cache = args.out / "rows.jsonl"
    done = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            row = json.loads(line)
            done[row["pr"]] = row
    prs = json.loads(
        _sh(
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--limit",
            str(args.limit),
            "--json",
            "number,mergeCommit,createdAt",
        )
    )
    excluded = {"budget_not_ok": 0, "no_merge_commit": 0, "diff_error": 0}
    rows = []
    with cache.open("a") as fh:
        for p in prs:
            n = p["number"]
            if n in done:
                rows.append(done[n])
                continue
            sha = (p.get("mergeCommit") or {}).get("oid")
            if not sha:
                excluded["no_merge_commit"] += 1
                continue
            try:
                diff = _sh("git", "diff", "-M", f"{sha}^1", sha)
            except subprocess.CalledProcessError:
                excluded["diff_error"] += 1
                continue
            counted = pr_shape.count_diff(diff)
            try:
                budget = json.loads(
                    _sh(
                        sys.executable,
                        str(_SCRIPTS / "review_budget.py"),
                        "--repo",
                        repo,
                        "--pr",
                        str(n),
                    )
                )
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                budget = {}
            if budget.get("status") != "ok":
                excluded["budget_not_ok"] += 1
                continue
            row = {
                "pr": n,
                "counted": counted["counted"],
                "plain": plain_count(diff, counted["excluded"]),
                "rounds": budget["count"],
                "era": "post" if p["createdAt"] >= cutover[:19] else "pre",
            }
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            rows.append(row)
    report: dict = {"repo": repo, "population": len(prs), "used": len(rows), "excluded": excluded}
    for era in ("all", "pre", "post"):
        sub = rows if era == "all" else [r for r in rows if r["era"] == era]
        tc, tp = bucket_table(sub, "counted"), bucket_table(sub, "plain")
        report[era] = {
            "n": len(sub),
            "rho_counted": round(
                spearman([r["counted"] for r in sub], [r["rounds"] for r in sub]), 3
            ),
            "rho_plain": round(spearman([r["plain"] for r in sub], [r["rounds"] for r in sub]), 3),
            "counted": tc,
            "plain": tp,
            "thresholds_counted": implied_thresholds(tc),
            "thresholds_plain": implied_thresholds(tp),
        }
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
