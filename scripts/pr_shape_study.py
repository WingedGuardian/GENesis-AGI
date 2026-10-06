#!/usr/bin/env python3
"""Re-derive the PR size bands from merged history, using ``pr_shape``.

The bands in ``pr_shape.py`` (``SHAPE_AT`` / ``OVERRIDE_AT``) came from a study
whose counting method was not recorded. This script re-runs it with the
counter that applies them, so the derivation can be repeated:

1. Take the most recently created PRs merged into the default branch
   (``--limit``, default 400).
2. Size each one by its squash commit, ``git diff -M <merge>^1 <merge>``,
   counted with ``pr_shape.count_diff``. A merge that is not a squash (more
   than one parent, or a multi-commit PR whose commit subject lacks ``(#N)``)
   is excluded, because one commit would not be the whole PR.
3. Read each one's review rounds with ``scripts/review_budget.py``. A PR whose
   budget status is not ``ok`` is excluded, and the exclusion is counted.
4. Report the median rounds per counted-size bucket, a Spearman rank
   correlation, and the thresholds this rule implies:
   - shape: the lower edge of the first bucket (n >= 5) whose median reaches
     3 rounds, the point where the commit gate's round-2 stop has fired;
   - override: the first such bucket whose median reaches 4, the terminal round.

Reviews before ``ROUND_RULE_CUTOVER_ISO`` follow the legacy rule (every head
the primary reviewer saw, clean ones included). Results are reported overall
and for two CREATION-DATE cohorts, PRs created before and after the cutover.
A PR created before it but reviewed after carries rounds under both rules, so
the cohorts approximate the rule eras rather than separate them. A plain size
(``count_diff``'s ``plain``: every non-blank changed line on the same sides,
comments kept, moves not paired) is reported beside the counted one.

Run from a checkout of the repository being studied, with its default branch
fetched; it needs ``gh`` auth. The repository is always the checkout's own.
Writes ``rows.jsonl`` (resumable; each row names its repository, and rows from
another repository are refused) and ``report.json`` to ``--out``.
"""

from __future__ import annotations

import argparse
import datetime
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
    (1000, None),
]
MIN_BUCKET_N = 5
#: Bumped whenever a cached row's fields or their meaning change; a cache from
#: another schema is refused rather than mixed into a report.
ROW_SCHEMA = 2


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation with average ranks for ties.

    None when it is undefined: fewer than two rows, or a constant variable.
    """

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
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _p75(r: list[int]) -> float:
    """75th percentile by linear interpolation between order statistics.

    That is ``statistics.quantiles(method="inclusive")``, Hyndman-Fan type 7, and
    its 50th percentile equals ``statistics.median``, so the two columns share one
    definition. The default ``exclusive`` method was rejected: on ``[1, 4]`` it
    returns 4.75, outside the observed range, which misleads in sparse buckets.
    """
    if len(r) == 1:
        return float(r[0])
    return statistics.quantiles(r, n=4, method="inclusive")[2]


def bucket_table(rows: list[dict], key: str) -> list[dict]:
    """Rounds per half-open size bucket ``[lo, hi)`` of ``rows[key]``.

    Each bucket reports n, the median and p75 rounds, and how many of its PRs ran
    past round 4 (the terminal round), the tail a median alone hides.
    """
    out = []
    for lo, hi in BUCKETS:
        r = sorted(x["rounds"] for x in rows if x[key] >= lo and (hi is None or x[key] < hi))
        out.append(
            {
                "bucket": f"[{lo},{hi})" if hi is not None else f"[{lo},+)",
                "lo": lo,
                "n": len(r),
                "median": float(statistics.median(r)) if r else None,
                "p75": _p75(r) if r else None,
                "past_round_4": sum(1 for x in r if x > 4),
            }
        )
    return out


def implied_thresholds(table: list[dict]) -> dict:
    """Shape and override points: the first bucket edges whose median reaches 3
    and 4 rounds, among buckets with at least ``MIN_BUCKET_N`` PRs.

    Each point carries a status, because a missing point has two different
    meanings: ``reached``; ``not_reached`` (no bucket's median gets there);
    ``insufficient_n`` (only a bucket too small to judge gets there).
    """
    out: dict = {}
    for name, level in (("shape", 3), ("override", 4)):
        value, status = None, "not_reached"
        for row in table:
            if row["median"] is None or row["median"] < level:
                continue
            if row["n"] < MIN_BUCKET_N:
                status = "insufficient_n"
                continue
            value, status = row["lo"], "reached"
            break
        out[name] = value
        out[f"{name}_status"] = status
    return out


def cohort_of(created: str, cutover: str) -> str:
    """``post`` when the PR was created at or after the round-rule cutover."""

    def parse(ts: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))

    return "post" if parse(created) >= parse(cutover) else "pre"


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, cwd=_ROOT).stdout


def _git(*args: str) -> str:
    """A history read through ``review_scope``'s hardened runner, the same one the
    ``pr_shape`` CLI uses: no attributes file, textconv, external diff or replace
    objects, and git's own environment overrides removed. Each of those can empty or
    change a read at exit 0. Raises ``CalledProcessError`` when the read fails, so a
    failed read is excluded and counted, never measured as an empty change.
    """
    scope = pr_shape._load_sibling("review_scope.py", "_review_scope_for_pr_shape")
    argv = list(args) if args[0] == "diff" else ["--no-replace-objects", *args]
    out = scope._git(argv, str(_ROOT))
    if out is None:
        raise subprocess.CalledProcessError(1, ["git", *argv])
    return out


def _is_squash(sha: str, pr: int, n_commits) -> bool:
    """True when ``sha`` is the PR's squash commit: one parent, and either GitHub's
    squash subject ``... (#N)`` or a one-commit PR. ``n_commits`` is a callable,
    asked only when the subject does not settle it (listing every PR's commits in
    one query exceeds GitHub's GraphQL node limit)."""
    parents, subject = _git("log", "-1", "--format=%P%n%s", sha).split("\n", 1)
    if len(parents.split()) != 1:
        return False
    return f"(#{pr})" in subject or n_commits() == 1


def _commit_count(repo: str, pr: int) -> int:
    return int(
        _sh(
            "gh",
            "pr",
            "view",
            str(pr),
            "-R",
            repo,
            "--json",
            "commits",
            "--jq",
            ".commits | length",
        )
    )


def load_cache(path: Path, repo: str) -> dict[int, dict]:
    """Rows already measured, keyed by PR number.

    A torn LAST line (an interrupted write) is dropped and the file truncated to
    the good rows; a bad line anywhere else, or a row from another repository,
    is an error, never silently reused.
    """
    if not path.exists():
        return {}
    lines = path.read_text().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    done: dict[int, dict] = {}
    for i, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                path.write_text("".join(f"{x}\n" for x in lines[:-1]))
                break
            raise SystemExit(f"{path}: line {i + 1} is not JSON") from None
        if row.get("repo") != repo:
            raise SystemExit(f"{path}: line {i + 1} is from {row.get('repo')!r}, not {repo!r}")
        if row.get("schema") != ROW_SCHEMA:
            raise SystemExit(
                f"{path}: line {i + 1} has row schema {row.get('schema')!r}, not {ROW_SCHEMA}; "
                "use a fresh --out directory"
            )
        done[row["pr"]] = row
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    args = ap.parse_args(argv)
    repo, base = _sh(
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner,defaultBranchRef",
        "--jq",
        '.nameWithOwner + "\\n" + .defaultBranchRef.name',
    ).split()
    args.out.mkdir(parents=True, exist_ok=True)
    import review_budget  # noqa: PLC0415

    cutover = review_budget.ROUND_RULE_CUTOVER_ISO
    cache = args.out / "rows.jsonl"
    done = load_cache(cache, repo)
    prs = json.loads(
        _sh(
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--base",
            base,
            "--limit",
            str(args.limit),
            "--json",
            "number,mergeCommit,createdAt,mergedAt",
        )
    )
    excluded = {"budget_not_ok": 0, "no_merge_commit": 0, "diff_error": 0, "not_squash": 0}
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
                squash = _is_squash(sha, n, lambda n=n: _commit_count(repo, n))
                diff = _git("diff", "--no-color", "-M", f"{sha}^1", sha)
            except subprocess.CalledProcessError:
                excluded["diff_error"] += 1
                continue
            if not squash:
                excluded["not_squash"] += 1
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
                "schema": ROW_SCHEMA,
                "repo": repo,
                "pr": n,
                "counted": counted["counted"],
                "plain": counted["plain"],
                "rounds": budget["count"],
                "created": p["createdAt"],
                "merged": p.get("mergedAt"),
            }
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            rows.append(row)

    def rho(sub: list[dict], key: str) -> float | None:
        r = spearman([x[key] for x in sub], [x["rounds"] for x in sub])
        return None if r is None else round(r, 3)

    report: dict = {
        "repo": repo,
        "base": base,
        "population": len(prs),
        "used": len(rows),
        "excluded": excluded,
        "cohorts": "by creation date against ROUND_RULE_CUTOVER_ISO",
        "zero_round_prs": sum(1 for r in rows if r["rounds"] == 0),
        "past_round_4": sum(1 for r in rows if r["rounds"] > 4),
        "max_rounds": max((r["rounds"] for r in rows), default=None),
    }
    for cohort in ("all", "pre", "post"):
        sub = (
            rows
            if cohort == "all"
            else [r for r in rows if cohort_of(r["created"], cutover) == cohort]
        )
        tc, tp = bucket_table(sub, "counted"), bucket_table(sub, "plain")
        report[cohort] = {
            "n": len(sub),
            "rho_counted": rho(sub, "counted"),
            "rho_plain": rho(sub, "plain"),
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
