"""Harvest merged-PR review findings into observations (dev-quality component g).

Pulls the last N merged PRs via the gh CLI, collects the review findings the
bots left on them, parses a severity per finding, and upserts one observation
per PR (deterministic id → idempotent re-harvest). The weekly ``dev_quality``
J-9 dimension reads these rows.

HARD-WON PRODUCTION LESSON — where the findings actually live: the Codex
review bot posts its findings as INLINE review comments, which only
``gh api repos/<owner>/<repo>/pulls/N/comments`` returns.
``gh pr view --json reviews,comments`` MISSES them entirely (``comments`` is
issue-level conversation, ``reviews`` carries only the top-level review
bodies). Do not "simplify" this back to ``gh pr view``.

The ``/reviews`` endpoint is still queried separately: the structural review
bot posts reviews with state COMMENTED, so the per-PR review count is a
distinct coverage signal from the finding count.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from genesis.db.crud import observations as obs_crud

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

logger = logging.getLogger(__name__)

# 30s per gh call: each is one GitHub API round-trip (network-bound, normally
# <2s). A hung call must not stall the weekly learning-scheduler job; the
# total run is bounded by ~2 calls per PR x the PR limit, so worst case stays
# well under the scheduler's misfire grace.
_GH_TIMEOUT_S = 30

# 90 days: long enough to cover any J-9 lookback window, short enough that
# re-harvested rows don't accumulate forever if a repo goes quiet.
_EXPIRES_DAYS = 90

# Severity vocab, lenient + case-insensitive. P1/P2/P3 (bare or bracketed —
# ``\b`` treats ``[``/``]`` as boundaries) map to the same canonical buckets
# as the spelled-out labels. A comment matching neither is ``unlabeled`` —
# an honest bucket, never a guess.
_P_LEVEL_RE = re.compile(r"\bP([123])\b", re.IGNORECASE)
_WORD_LEVEL_RE = re.compile(r"\b(BLOCKER|SHOULD-FIX|NOTE)\b", re.IGNORECASE)
_P_TO_BUCKET = {"1": "blocker", "2": "should_fix", "3": "note"}
_WORD_TO_BUCKET = {"blocker": "blocker", "should-fix": "should_fix", "note": "note"}

SEVERITY_BUCKETS = ("blocker", "should_fix", "note", "unlabeled")


async def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run a gh CLI command, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GH_TIMEOUT_S,
        )
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        return 124, "", f"timed out after {_GH_TIMEOUT_S}s: {' '.join(argv)}"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def parse_severity(body: str) -> str:
    """Map a review-comment body to a canonical severity bucket."""
    m = _P_LEVEL_RE.search(body or "")
    if m:
        return _P_TO_BUCKET[m.group(1)]
    m = _WORD_LEVEL_RE.search(body or "")
    if m:
        return _WORD_TO_BUCKET[m.group(1).lower()]
    return "unlabeled"


def _parse_concat_json(text: str) -> list[dict]:
    """Parse gh output that may be several concatenated JSON documents.

    ``gh api --paginate`` on an array endpoint emits one JSON array PER PAGE,
    back to back — the combined output is not a single valid document.
    raw_decode walks the stream and flattens the arrays.
    """
    decoder = json.JSONDecoder()
    items: list[dict] = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        value, idx = decoder.raw_decode(text, idx)
        if isinstance(value, list):
            items.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            items.append(value)
    return items


async def _harvest_one_pr(
    db: aiosqlite.Connection, runner: Runner, repo: str, pr: dict, now: datetime,
) -> dict:
    """Fetch findings + review count for one PR and upsert its observation.

    Returns {"findings": [...], "review_count": int}. Raises on gh failure —
    the caller logs and skips (one bad PR must not kill the harvest).
    """
    number = int(pr["number"])

    rc, out, err = await runner(
        ["gh", "api", f"repos/{repo}/pulls/{number}/comments", "--paginate"],
    )
    if rc != 0:
        raise RuntimeError(f"inline-comments fetch failed (rc={rc}): {err.strip()}")
    comments = _parse_concat_json(out)

    rc, out, err = await runner(
        ["gh", "api", f"repos/{repo}/pulls/{number}/reviews", "--paginate"],
    )
    if rc != 0:
        raise RuntimeError(f"reviews fetch failed (rc={rc}): {err.strip()}")
    review_count = len(_parse_concat_json(out))

    findings = []
    for c in comments:  # one finding per inline review comment
        body = c.get("body") or ""
        findings.append({
            "severity": parse_severity(body),
            "author": (c.get("user") or {}).get("login"),
            "path": c.get("path"),
            "excerpt": " ".join(body.split())[:120],
        })

    content = json.dumps({
        "pr": number,
        "title": pr.get("title"),
        "merged_at": pr.get("mergedAt"),
        "review_count": review_count,
        "findings": findings,
    })
    # Deterministic id → the weekly re-harvest of a still-in-window PR
    # updates its row in place instead of duplicating it. Repo-scoped so a
    # repo rename/switch can never silently upsert PR #N of one repo over
    # PR #N of another.
    await obs_crud.upsert(
        db,
        id=f"prrev-{repo.replace('/', '-')}-{number}",
        source="recon",
        type="pr_review_findings",
        category="pr_review_findings",
        content=content,
        priority="low",
        created_at=now.isoformat(),
        # Explicit (not auto-TTL): the retention contract is this module's,
        # not the observation-type table's default.
        expires_at=(now + timedelta(days=_EXPIRES_DAYS)).isoformat(),
    )
    return {"findings": findings, "review_count": review_count}


async def harvest_pr_review_findings(
    db: aiosqlite.Connection,
    *,
    repo: str | None = None,
    lookback_days: int = 30,
    limit: int = 50,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Harvest review findings for recently merged PRs into observations.

    ``runner`` is an injectable ``async (argv) -> (rc, stdout, stderr)``
    subprocess callable (tests pass a fake); defaults to the gh CLI.

    Returns a summary dict {repo, prs_seen, findings_total, by_severity,
    errors}. A failed repo-resolve or PR-list returns {"error": ...} without
    raising; per-PR failures are logged, recorded in ``errors``, and skipped.
    """
    run = runner or _default_runner
    now = datetime.now(UTC)

    if repo is None:
        rc, out, err = await run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        )
        repo = out.strip()
        if rc != 0 or not repo:
            return {"error": f"repo resolve failed (rc={rc}): {err.strip()}"}

    merged_since = (now - timedelta(days=lookback_days)).date().isoformat()
    rc, out, err = await run([
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--search", f"merged:>={merged_since}",
        "--json", "number,mergedAt,title",
        "--limit", str(limit),
    ])
    if rc != 0:
        return {"error": f"pr list failed (rc={rc}): {err.strip()}"}
    try:
        prs = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"pr list returned invalid JSON: {exc}"}

    findings_total = 0
    by_severity = dict.fromkeys(SEVERITY_BUCKETS, 0)
    errors: list[str] = []
    for pr in prs:
        try:
            result = await _harvest_one_pr(db, run, repo, pr, now)
        except Exception as exc:
            # One bad PR (deleted branch, API 5xx, timeout) must not kill
            # the whole harvest — record and move on.
            msg = f"PR #{pr.get('number')}: {exc}"
            logger.warning("pr_review_harvest skipped %s", msg)
            errors.append(msg)
            continue
        for f in result["findings"]:
            findings_total += 1
            by_severity[f["severity"]] += 1

    summary = {
        "repo": repo,
        "prs_seen": len(prs),
        "findings_total": findings_total,
        "by_severity": by_severity,
        "errors": errors,
    }
    logger.info("pr_review_harvest: %s", summary)
    return summary
