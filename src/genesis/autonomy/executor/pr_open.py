"""Server-side draft PR opening for build-lane deliveries.

Runs `gh pr create --draft` as a SERVER subprocess after the engine has
pushed a scope-gated build branch. This is deliberately NOT done from
inside CC step sessions — the repo hooks block PR creation there, and the
server process is the trust boundary that already performed the push.

Failure here is non-fatal to delivery: the branch is already pushed, so a
failed PR-open degrades to "branch delivered, open the PR manually" (the
error is recorded on the task and surfaced in the notification).

Adapted from contribution/pr_opener.py's gh helpers (availability, auth,
repo resolution) with async subprocess plumbing to match the engine.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Network CLI call with no external watchdog: a hung `gh` would wedge the
# executor's delivery path (and its semaphore) permanently — the one case
# the project timeout policy carves out for a timeout. Legitimate
# `gh pr create` completes in seconds; 300s is two orders of magnitude
# above that.
_GH_TIMEOUT_S = 300


@dataclass(frozen=True)
class PrOpenResult:
    """Outcome of a draft-PR open attempt (url on success, error otherwise)."""

    ok: bool
    pr_url: str = ""
    error: str = ""
    dry_run_cmd: str = ""


async def _run(
    args: list[str], *, cwd: Path, timeout_s: float = _GH_TIMEOUT_S,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout_s}s"
    return (
        proc.returncode or 0,
        (out or b"").decode(errors="replace"),
        (err or b"").decode(errors="replace"),
    )


async def open_draft_pr(
    *,
    worktree_path: Path,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    dry_run: bool = False,
) -> PrOpenResult:
    """Open a draft PR for *branch* against *base* in the worktree's repo.

    ``dry_run=True`` validates gh availability/auth and repo resolution,
    then returns the command that WOULD run — used by the build-lane
    rehearsal path.
    """
    if shutil.which("gh") is None:
        return PrOpenResult(ok=False, error="gh CLI not available on PATH")

    rc, out, err = await _run(["gh", "auth", "status"], cwd=worktree_path)
    if rc != 0:
        return PrOpenResult(
            ok=False, error=f"gh not authenticated: {(err or out).strip()[:200]}",
        )

    rc, out, err = await _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=worktree_path,
    )
    if rc != 0 or not out.strip():
        return PrOpenResult(
            ok=False, error=f"could not resolve target repo: {(err or out).strip()[:200]}",
        )
    target_repo = out.strip()

    cmd = [
        "gh", "pr", "create",
        "--draft",
        "--head", branch,
        "--base", base,
        "--title", title,
        "--body", body,
    ]
    if dry_run:
        logger.info(
            "pr_open dry run for %s -> %s (repo %s)", branch, base, target_repo,
        )
        return PrOpenResult(ok=True, dry_run_cmd=" ".join(cmd))

    rc, out, err = await _run(cmd, cwd=worktree_path)
    if rc != 0:
        return PrOpenResult(
            ok=False, error=f"gh pr create failed (rc={rc}): {(err or out).strip()[:300]}",
        )

    # gh prints the PR URL as the last non-empty stdout line.
    url = ""
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("https://"):
            url = line.strip()
            break
    if not url:
        return PrOpenResult(
            ok=False, error=f"gh pr create succeeded but no URL in output: {out.strip()[:200]}",
        )
    logger.info("Opened draft PR %s for branch %s", url, branch)
    return PrOpenResult(ok=True, pr_url=url)


def build_pr_title(description: str) -> str:
    """Draft-PR title for a build-lane task."""
    clean = " ".join(description.split())
    if len(clean) > 70:
        clean = clean[:67] + "..."
    return f"[build-lane] {clean}"


def build_pr_body(*, task_id: str, plan_path: str, scope_gate_json: str) -> str:
    """Draft-PR body: provenance + the scope-gate verdict, nothing fancier.

    The build lane's reporting layer links the candidate/verdict context;
    this body only needs to make the PR self-describing and mark it as
    autonomous output awaiting human review.
    """
    return (
        "## Autonomous build (draft)\n\n"
        "Built by the Genesis capability-build lane. "
        "**Draft by design — human review + merge required.**\n\n"
        f"- Task: `{task_id}`\n"
        f"- Plan: `{plan_path}`\n"
        f"- Scope gate: `{scope_gate_json}`\n"
    )
