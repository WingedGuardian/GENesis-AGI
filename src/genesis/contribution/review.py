"""Phase 6 contribution — adversarial review fallback chain.

First-success chain (NOT fan-out):

1. `codex exec` with medium reasoning effort — primary, uses the
   codex binary if installed. Gives free-form analysis of the diff.
2. `superpowers:code-reviewer` subagent — only reachable from inside
   a CC session (via the Task tool). Standalone CLI invocations
   skip this link.
3. Genesis-native adversarial review — 6.2+ placeholder. Returns
   "unavailable" in MVP.

Each link has its own timeout. First success wins — we stop at the
first link that returns a result. Full chain failure still proceeds
to submission with `Review: unavailable` noted in the PR body.

Design note: reviews are NON-BLOCKING signal, never a gate. The
sanitizer is the safety wall. If a reviewer flags concerns, they
appear in the PR body for the maintainer to triage, not as a
blocking decision here.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .findings import ReviewResult

logger = logging.getLogger(__name__)

CODEX_TIMEOUT = 300  # 5 minutes — generous, codex can think hard
CC_REVIEWER_TIMEOUT = 300
NATIVE_TIMEOUT = 120

_REVIEW_PROMPT = """You are an adversarial code reviewer for a community \
contribution to an open-source project.

Read the following unified diff and produce a brief review. Focus on:
- Correctness: does the change actually fix the stated bug?
- Regressions: could this break something else?
- Security: injection, auth bypass, data exposure, timing attacks
- Fit: does the change align with the surrounding code style?

Be specific and adversarial. If the diff looks clean, say so and stop.
Do not repeat the diff back.

Respond in plain text, max 400 words. End with a line:
VERDICT: PASS  — or —  VERDICT: FAIL (<short reason>)

--- DIFF ---

{diff}
"""


def _parse_verdict(text: str) -> tuple[bool, int, str]:
    """Extract (passed, finding_count, summary) from reviewer output.

    Parses the trailing `VERDICT: PASS/FAIL` line. Counts
    heuristic mentions of "issue" / "concern" / "bug" / "finding"
    for an approximate finding count.
    """
    lower = text.lower()
    passed = "verdict: pass" in lower or "verdict:pass" in lower
    # Rough finding count — count distinct blocks starting with common
    # review markers. Reviewers produce free-form text so this is
    # approximate by design.
    markers = ("issue:", "concern:", "bug:", "finding:", "- [", "1.", "2.", "3.")
    seen: set[int] = set()
    for m in markers:
        pos = 0
        while True:
            idx = lower.find(m, pos)
            if idx < 0:
                break
            seen.add(idx)
            pos = idx + len(m)
    finding_count = len(seen) if not passed else 0
    # Summary = last 200 chars, truncated at the verdict line
    summary_end = lower.rfind("verdict:")
    summary_src = text[:summary_end].strip() if summary_end > 0 else text
    summary = summary_src[-400:].strip()
    return passed, finding_count, summary


def _try_codex(diff: str, *, timeout: int = CODEX_TIMEOUT) -> ReviewResult | None:
    """Try the codex exec path. Returns None if codex is missing or errors."""
    if shutil.which("codex") is None:
        logger.info("review: codex binary not on PATH — skipping codex link")
        return None

    prompt = _REVIEW_PROMPT.format(diff=diff)
    # Pass the prompt via stdin, not argv. Large diffs can blow past
    # the single-argv length limit (P2-2 from the 6.1b.2 review) and
    # argv contents leak through `ps auxwww`. Codex exec with `-`
    # reads the prompt from stdin.
    try:
        proc = subprocess.run(
            [
                "codex", "exec", "-",
                "-s", "read-only",
                "-c", 'model_reasoning_effort="medium"',
                "--json",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("review: codex exec timed out after %ss", timeout)
        return None
    except FileNotFoundError:
        return None

    if proc.returncode != 0:
        logger.warning(
            "review: codex exec rc=%s, stderr=%r",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return None

    # codex --json emits JSONL; we want the final agent_message text
    pieces: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                pieces.append(item["text"])

    output = "\n".join(pieces).strip()
    if not output:
        logger.warning("review: codex output was empty after JSONL parse")
        return None

    passed, count, summary = _parse_verdict(output)
    return ReviewResult(
        available=True,
        reviewer="codex",
        passed=passed,
        finding_count=count,
        summary=summary,
        raw=output,
    )


def _try_cc_reviewer(diff: str, *, timeout: int = CC_REVIEWER_TIMEOUT) -> ReviewResult | None:
    """Try the superpowers:code-reviewer subagent.

    Only reachable from inside a CC session via the Task tool. For
    standalone CLI invocations we have no channel to dispatch a
    subagent, so this link returns None in MVP. Post-MVP: when the
    contribution flow runs inside a CC session with tool access, the
    caller can inject a subagent-runner callable.
    """
    # Explicit opt-out for non-CC environments. No heuristic to detect
    # "am I inside CC" from inside a subprocess — the parent would
    # have to tell us via env var.
    logger.info("review: cc-reviewer link not reachable from subprocess context")
    return None


def _try_genesis_native(diff: str, *, timeout: int = NATIVE_TIMEOUT) -> ReviewResult | None:
    """Placeholder for Phase 6.2+ Genesis-native adversarial review.

    Returns None in MVP — the native review call site isn't wired yet.
    """
    return None


def run_review_chain(
    diff: str,
    *,
    skip_codex: bool = False,
    skip_cc_reviewer: bool = False,
    skip_native: bool = False,
) -> ReviewResult:
    """Run the review chain. First success wins; empty chain → unavailable.

    Args:
        diff: The unified diff to review.
        skip_*: Let callers disable specific links (used by tests and
            CLI opt-outs).

    Returns:
        ReviewResult. On full-chain failure, `available=False`.
    """
    if not skip_codex:
        r = _try_codex(diff)
        if r is not None:
            return r

    if not skip_cc_reviewer:
        r = _try_cc_reviewer(diff)
        if r is not None:
            return r

    if not skip_native:
        r = _try_genesis_native(diff)
        if r is not None:
            return r

    return ReviewResult(
        available=False,
        reviewer=None,
        passed=False,
        finding_count=0,
        summary="review chain unavailable",
        raw="",
    )


def write_review_log(result: ReviewResult, path: Path) -> None:
    """Persist a review result as JSON for post-hoc inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8",
    )
