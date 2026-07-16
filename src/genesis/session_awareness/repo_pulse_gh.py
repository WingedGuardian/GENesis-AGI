"""Merged-PR enumeration for the repo-pulse worker (gh CLI, injectable).

Clone of the ``pr_review_harvest`` gh pattern with two pulse-specific
hardenings, both live-verified during PR-4 due diligence:

1. **The repo slug is resolved LIVE, never from config.** A configured slug
   can name a real-but-wrong repo and return PLAUSIBLE STALE data (the
   working-repo config entry answered with April's PRs — zero error, zero
   matches, permanently silent). ``gh repo view`` resolves from the git
   remote of the cwd, so the default runner pins ``cwd=repo_root()``.
2. **A capped window is loud, never silent.** GitHub search cannot sort by
   mergedAt ascending, so when ``len(prs) == limit`` the enumeration MAY
   have dropped older PRs inside the window — the result carries
   ``limit_hit=True`` and the worker records it on the run row. PR velocity
   here is ~100/week; the default limit (200) covers ~2 weeks of catch-up.

Nothing here writes anywhere; errors return ``{"error": ...}`` without
raising (the worker records them as failed runs and leaves the cursor).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from genesis.env import repo_root

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

logger = logging.getLogger(__name__)

# 30s per gh call: one GitHub API round-trip (network-bound, normally <2s).
# A hung call would sit on pulse.lock and starve every later session
# boundary's pulse until process death — the exact raw-subprocess-with-no-
# external-watchdog case the timeout policy carves out.
_GH_TIMEOUT_S = 30

PR_FIELDS = "number,title,body,mergedAt"


async def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run a gh CLI command from the repo root, returning (rc, stdout, stderr).

    cwd is pinned to the repo so ``gh repo view`` resolves the slug from
    THIS repo's git remote regardless of where the detached worker was
    spawned from (hardening 1 above).
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(repo_root()),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh call timed out after {_GH_TIMEOUT_S}s"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def resolve_repo(runner: Runner | None = None) -> str | None:
    """Live ``owner/name`` slug of the repo the worker runs against."""
    run = runner or _default_runner
    rc, out, err = await run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    slug = out.strip()
    if rc != 0 or not slug:
        logger.warning("repo_pulse slug resolve failed (rc=%s): %s", rc, err.strip())
        return None
    return slug


async def list_merged_prs(
    *,
    since_date: str,
    limit: int = 200,
    repo: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Enumerate PRs merged on/after ``since_date`` (YYYY-MM-DD, date-granular).

    Returns ``{"repo", "prs", "limit_hit"}`` with prs sorted by mergedAt
    ASCENDING (cursor math processes oldest-first), or ``{"error": ...}``.
    Rows missing an int ``number`` or a ``mergedAt`` are dropped — gh's
    contract violation, not a crash. The date-granular search re-covers up
    to a day behind the cursor by design; the caller filters client-side
    against its exact ISO watermark.
    """
    run = runner or _default_runner
    if repo is None:
        repo = await resolve_repo(run)
        if repo is None:
            return {"error": "repo slug resolve failed"}
    rc, out, err = await run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--search",
            f"merged:>={since_date}",
            "--json",
            PR_FIELDS,
            "--limit",
            str(limit),
        ]
    )
    if rc != 0:
        return {"error": f"pr list failed (rc={rc}): {err.strip()[:400]}"}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"pr list returned invalid JSON: {exc}"}
    if not isinstance(raw, list):
        return {"error": "pr list returned a non-list payload"}
    prs = [
        pr
        for pr in raw
        if isinstance(pr, dict)
        and isinstance(pr.get("number"), int)
        and isinstance(pr.get("mergedAt"), str)
        and pr["mergedAt"]
    ]
    prs.sort(key=lambda p: p["mergedAt"])
    return {"repo": repo, "prs": prs, "limit_hit": len(raw) >= limit}
