"""Adversarial quality gate -- dual LLM review for task deliverables.

Implements the quality gate from the design doc:
1. Programmatic checks (basic validation)
2. Fresh-eyes review (call site 17, cross-vendor API)
3. Adversarial verification (tool-capable chain: Codex -> CC invoker -> API)

If programmatic checks fail, LLM review is skipped entirely.
If cross-vendor routing fails after retries, review is skipped with
a warning (Amendment #5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from genesis.cc.invoker import CCInvoker

logger = logging.getLogger(__name__)

_CALL_SITE_PLAN = "27_pre_execution_assessment"
_CALL_SITE_FRESH = "17_fresh_eyes_review"
_CALL_SITE_ADVERSARIAL = "20_adversarial_counterargument"
_MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Router protocol (same as decomposer.py)
# ---------------------------------------------------------------------------


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewResult:
    """Result of pre-execution plan review."""

    passed: bool
    gaps: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerifyResult:
    """Result of post-execution adversarial verification."""

    passed: bool
    programmatic_issues: list[str] = field(default_factory=list)
    fresh_eyes_feedback: str | None = None
    adversarial_feedback: str | None = None
    skipped_reason: str | None = None
    iteration: int = 0


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


class TaskReviewer:
    """Adversarial quality gate using cross-vendor LLM review.

    Plan review uses CC invoker (Opus) when available, falling back to
    call site 27 via route_call.  Post-execution verification uses
    call sites 17 (fresh eyes) and 20 (adversarial counterargument),
    which are configured for cross-vendor routing.
    """

    def __init__(
        self,
        *,
        router: _Router,
        invoker: CCInvoker | None = None,
    ) -> None:
        self._router = router
        self._invoker = invoker

    # --- Plan review (pre-execution) ------------------------------------

    async def review_plan(
        self,
        plan_content: str,
        task_description: str,
    ) -> ReviewResult:
        """Pre-execution plan review via CC invoker (Opus) or call site 27.

        Prefers CC invoker for Opus-quality review. Falls back to
        route_call if invoker unavailable or fails. On total failure,
        passes the plan through rather than blocking execution.
        """
        prompt = self._build_plan_review_prompt(plan_content, task_description)

        # Primary path: CC invoker with Opus
        if self._invoker is not None:
            try:
                content = await self._review_plan_via_invoker(prompt)
                if content is not None:
                    return self._parse_plan_review(content)
            except Exception:
                logger.warning(
                    "CC invoker plan review failed, falling back to route_call",
                    exc_info=True,
                )

        # Fallback: route_call to call site 27
        messages = [{"role": "user", "content": prompt}]
        try:
            result = await self._router.route_call(_CALL_SITE_PLAN, messages)
        except Exception:
            logger.error(
                "Plan review routing exception", exc_info=True,
            )
            return ReviewResult(passed=True)

        if not result.success or not result.content:
            logger.warning(
                "Plan review routing failed (success=%s), passing plan through",
                getattr(result, "success", None),
            )
            return ReviewResult(passed=True)

        return self._parse_plan_review(result.content)

    async def _review_plan_via_invoker(self, prompt: str) -> str | None:
        """Run plan review via CC invoker (Opus). Returns text or None."""
        from genesis.cc.types import CCInvocation, CCModel, EffortLevel, background_session_dir

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.OPUS,
            effort=EffortLevel.HIGH,
            timeout_s=600,
            working_dir=background_session_dir(),
            skip_permissions=True,
        )
        output = await self._invoker.run(invocation)
        if output.is_error:
            logger.warning(
                "CC invoker plan review returned error: %s",
                output.error_message or output.text[:200],
            )
            return None
        return output.text

    # --- Deliverable verification (post-execution) ----------------------

    async def verify_deliverable(
        self,
        deliverable: str,
        requirements: str,
        *,
        task_type: str = "code",
        iteration: int = 0,
        worktree_path: Path | None = None,
    ) -> VerifyResult:
        """Post-execution verification with tool-capable adversarial gate.

        Gate 1: Programmatic checks (basic validation).
        Gate 2: Fresh-eyes review (call site 17, cross-vendor API).
        Gate 3: Adversarial verification via tool-capable chain:
                Codex (GPT, cross-vendor) -> CC invoker (Sonnet) -> API fallback.

        If programmatic checks fail, LLM gates are skipped.
        If both LLM routes fail, delivers with a warning (Amendment #5).

        *worktree_path*: When the task executed in a git worktree, pass the
        path so tool-capable reviewers (Codex, CC invoker) can inspect the
        actual changed files rather than the repo root / main branch.
        """
        # Gate 1 -- programmatic checks
        issues = self._programmatic_checks(deliverable, task_type)
        if issues:
            return VerifyResult(
                passed=False,
                programmatic_issues=issues,
                iteration=iteration,
            )

        # Gate 2 -- fresh-eyes review (API, enriched deliverable)
        fresh_eyes = await self._llm_review(
            _CALL_SITE_FRESH, deliverable, requirements,
        )

        # Gate 3 -- adversarial verification (tool-capable chain)
        adversarial = await self._tool_capable_review(
            deliverable, requirements, worktree_path=worktree_path,
        )

        # Amendment #5: both routing fail -> deliver with warning
        if fresh_eyes is None and adversarial is None:
            logger.warning(
                "Both review models unavailable, delivering without adversarial review",
            )
            return VerifyResult(
                passed=True,
                skipped_reason=(
                    "Delivered without adversarial review "
                    "-- cross-vendor model unavailable"
                ),
                iteration=iteration,
            )

        # Assess pass/fail from available feedback
        passed = self._assess_feedback(fresh_eyes, adversarial)
        return VerifyResult(
            passed=passed,
            fresh_eyes_feedback=fresh_eyes,
            adversarial_feedback=adversarial,
            iteration=iteration,
        )

    # --- Tool-capable verification chain --------------------------------

    async def _tool_capable_review(
        self,
        deliverable: str,
        requirements: str,
        *,
        worktree_path: Path | None = None,
    ) -> str | None:
        """Run the tool-capable adversarial verification chain.

        First-success semantics (same pattern as contribution/review.py):
        1. Codex (GPT, cross-vendor, full tools) -- ``shutil.which`` guard
        2. CC invoker (Sonnet, same-vendor fallback, full tools)
        3. API call site 20 (last resort, text-only)

        Returns verdict text or ``None`` on total chain failure.

        *worktree_path*: passed to Codex (as ``cwd``) and CC invoker
        (as ``working_dir``) so they inspect the worktree, not repo root.

        # GROUNDWORK(per-step-verify): This method is extracted so future
        # per-step verification can reuse the same chain.  The follow-up
        # PR will call it from the step execution loop for steps where
        # ``StepType.verify_step`` is True.
        """
        # Link 1: Codex (cross-vendor, tool-capable)
        verdict = await self._verify_via_codex(
            deliverable, requirements, worktree_path=worktree_path,
        )
        if verdict is not None:
            return verdict

        # Link 2: CC invoker (Sonnet, tool-capable fallback)
        verdict = await self._verify_via_invoker(
            deliverable, requirements, worktree_path=worktree_path,
        )
        if verdict is not None:
            return verdict

        # Link 3: API call site 20 (text-only last resort)
        return await self._llm_review(
            _CALL_SITE_ADVERSARIAL, deliverable, requirements,
        )

    async def _verify_via_codex(
        self,
        deliverable: str,
        requirements: str,
        *,
        worktree_path: Path | None = None,
    ) -> str | None:
        """Run adversarial verification via ``codex exec`` subprocess.

        Returns verdict text or ``None`` if Codex is not installed or fails.
        Uses ``asyncio.create_subprocess_exec`` since review.py is async.

        *worktree_path*: when set, Codex runs with ``cwd`` pointing to the
        worktree so it can read the actual changed files rather than main.
        """
        if shutil.which("codex") is None:
            logger.info("review: codex not on PATH -- skipping codex link")
            return None

        prompt = self._build_active_verify_prompt(deliverable, requirements)

        # Resolve cwd: prefer worktree so Codex sees the changed files
        cwd = str(worktree_path) if worktree_path else None

        try:
            # Sandbox bypass required: bubblewrap namespace creation
            # fails inside containers (bwrap: Creating new namespace
            # failed: Permission denied).  --full-auto also fails.
            # Verification is read-only in intent; the bypass only
            # affects the sandbox layer, not the prompt instructions.
            proc = await asyncio.create_subprocess_exec(
                "codex", "exec", "-",
                "-c", 'model_reasoning_effort="medium"',
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout_bytes, stderr_bytes = await proc.communicate(
                input=prompt.encode("utf-8"),
            )
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("review: codex exec OS error", exc_info=True)
            return None

        if proc.returncode != 0:
            logger.warning(
                "review: codex exec rc=%s, stderr=%r",
                proc.returncode,
                (stderr_bytes.decode(errors="replace") or "")[:200],
            )
            return None

        # Parse JSONL output -- same pattern as contribution/review.py
        stdout = stdout_bytes.decode(errors="replace")
        pieces: list[str] = []
        for line in stdout.splitlines():
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

        logger.info("review: codex adversarial verification completed")
        return output

    async def _verify_via_invoker(
        self,
        deliverable: str,
        requirements: str,
        *,
        worktree_path: Path | None = None,
    ) -> str | None:
        """Run adversarial verification via CC invoker (Sonnet).

        Returns verdict text or ``None`` if invoker is unavailable or fails.

        *worktree_path*: when set, the CC session runs in the worktree
        directory so it can inspect the actual changed files.
        """
        if self._invoker is None:
            return None

        prompt = self._build_active_verify_prompt(deliverable, requirements)

        try:
            from genesis.cc.types import CCInvocation, CCModel, EffortLevel, background_session_dir

            invocation = CCInvocation(
                prompt=prompt,
                model=CCModel.SONNET,
                effort=EffortLevel.HIGH,
                skip_permissions=True,
                working_dir=str(worktree_path) if worktree_path else background_session_dir(),
            )
            output = await self._invoker.run(invocation)
            if output.is_error:
                logger.warning(
                    "CC invoker adversarial review returned error: %s",
                    output.error_message or output.text[:200],
                )
                return None
            return output.text
        except Exception:
            logger.warning(
                "CC invoker adversarial review failed",
                exc_info=True,
            )
            return None

    # --- Programmatic checks --------------------------------------------

    def _programmatic_checks(
        self,
        deliverable: str,
        task_type: str,
    ) -> list[str]:
        """Basic validation before LLM review. Empty list means pass."""
        issues: list[str] = []

        if not deliverable or not deliverable.strip():
            issues.append("Deliverable is empty")
            return issues

        lines = deliverable.strip().splitlines()
        if task_type == "code" and len(lines) < 3:
            issues.append(
                f"Code deliverable has only {len(lines)} line(s) "
                "-- suspiciously short"
            )

        return issues

    # --- LLM review with retry ------------------------------------------

    async def _llm_review(
        self,
        call_site: str,
        deliverable: str,
        requirements: str,
    ) -> str | None:
        """Single LLM review. Returns feedback text or None on routing failure."""
        prompt = self._build_verify_prompt(call_site, deliverable, requirements)

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                result = await self._router.route_call(
                    call_site,
                    [{"role": "user", "content": prompt}],
                )
                if result.success and result.content:
                    return result.content
            except Exception:
                logger.error(
                    "Review routing exception for %s (attempt %d/%d)",
                    call_site, attempt, _MAX_RETRIES,
                    exc_info=True,
                )

            logger.warning(
                "Review routing failed for %s (attempt %d/%d)",
                call_site, attempt, _MAX_RETRIES,
            )

        return None

    # --- Feedback assessment --------------------------------------------

    def _assess_feedback(
        self,
        fresh_eyes: str | None,
        adversarial: str | None,
    ) -> bool:
        """Determine overall pass/fail from LLM review feedback.

        Tries JSON parse first (looking for ``"verdict": "fail"``),
        then falls back to keyword heuristic.
        """
        for feedback in (fresh_eyes, adversarial):
            if feedback is None:
                continue

            text = feedback.strip()

            # Strip code fences
            match = _JSON_BLOCK_RE.search(text)
            if match:
                text = match.group(1).strip()

            # Try JSON parse
            with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
                data = json.loads(text)
                if isinstance(data, dict) and data.get("verdict") == "fail":
                    return False

            # Keyword heuristic fallback
            lower = feedback.lower()
            if '"verdict": "fail"' in lower or '"verdict":"fail"' in lower:
                return False

        return True

    # --- Prompt builders ------------------------------------------------

    @staticmethod
    def _build_plan_review_prompt(
        plan_content: str,
        task_description: str,
    ) -> str:
        return (
            "You are reviewing a task plan before it is submitted to an "
            "autonomous executor. Your job is to assess whether the plan "
            "is clear enough for the executor to act on.\n\n"
            "## What the Executor Already Handles\n\n"
            "Do NOT flag gaps in these areas — the executor manages them "
            "automatically:\n"
            "- **Timeouts and retries** — each step has configurable "
            "timeouts with automatic workaround recovery on failure\n"
            "- **Git worktrees** — code steps run in isolated worktrees, "
            "cleaned up automatically\n"
            "- **State machine** — task phases (reviewing → planning → "
            "executing → verifying → delivering) are managed by the engine\n"
            "- **Notifications** — Telegram alerts at each phase and on "
            "blockers\n"
            "- **Quality gates** — post-execution verification uses "
            "cross-vendor LLM review (fresh-eyes + adversarial)\n"
            "- **Failure escalation** — blocked steps are persisted to DB "
            "and escalated to the user\n"
            "- **Step decomposition** — the executor breaks high-level "
            "steps into typed sub-steps (research, code, analysis, "
            "synthesis, verification)\n\n"
            "## What You Should Review\n\n"
            "Focus on whether the plan gives the executor enough "
            "information to succeed:\n"
            "1. **Requirements clarity** — are the desired outcomes "
            "specific and unambiguous?\n"
            "2. **Success criteria** — are there testable conditions that "
            "define 'done'? This is critical.\n"
            "3. **Step feasibility** — can each step be accomplished by a "
            "CC session with standard tools (Read, Write, Edit, Bash, "
            "Grep, WebSearch)?\n"
            "4. **Missing context** — does the executor need information "
            "the plan doesn't provide (file paths, API details, "
            "credentials)?\n"
            "5. **Risks and failure modes** — are there foreseeable ways "
            "this could go wrong that the plan should address?\n"
            "6. **Scope** — is the task appropriately sized (max 8 steps "
            "after decomposition)?\n\n"
            "Steps should be written from the executor session's "
            "perspective — what the CC session will do, not what the user "
            "does.\n\n"
            "## Task Description\n"
            f"{task_description}\n\n"
            "## Plan\n"
            f"{plan_content}\n\n"
            "## Response Format\n"
            'Respond with a JSON object:\n'
            '{"passed": true/false, "gaps": ["list of genuine gaps"], '
            '"recommendations": ["list of recommendations"]}\n\n'
            "Only flag genuine gaps that would prevent the executor from "
            "completing the task. Do not flag infrastructure concerns the "
            "executor already handles."
        )

    @staticmethod
    def _build_verify_prompt(
        call_site: str,
        deliverable: str,
        requirements: str,
    ) -> str:
        is_fresh = call_site == _CALL_SITE_FRESH
        role = (
            "an independent reviewer"
            if is_fresh
            else "an adversarial critic trying to find flaws"
        )
        instruction = (
            "Review for correctness, completeness, and quality. "
            "Flag any issues you find."
            if is_fresh
            else "Try hard to find flaws, gaps, edge cases, and "
                 "potential failures. Be thorough and skeptical."
        )
        return (
            f"You are {role}. Evaluate whether the deliverable "
            "meets the requirements.\n\n"
            "## Requirements\n"
            f"{requirements}\n\n"
            "## Deliverable\n"
            f"{deliverable}\n\n"
            "## Instructions\n"
            f"{instruction}\n\n"
            'Respond with a JSON object:\n'
            '{"verdict": "pass" or "fail", '
            '"issues": ["list of specific issues"], '
            '"feedback": "overall assessment"}'
        )

    @staticmethod
    def _build_active_verify_prompt(
        deliverable: str,
        requirements: str,
    ) -> str:
        """Build prompt for tool-capable adversarial verification.

        Unlike ``_build_verify_prompt`` (text-only API models), this
        prompt instructs the reviewer to USE TOOLS to verify claims.
        """
        return (
            "You are an adversarial verifier. Your job is to ACTIVELY CHECK "
            "whether the deliverable meets the requirements. Do not just read "
            "the summary -- use your tools to verify:\n\n"
            "- If a file should exist, READ the file and check its contents\n"
            "- If a URL should be live, FETCH the URL\n"
            "- If tests should pass, RUN the tests\n"
            "- If code was written, READ and REVIEW the actual code\n\n"
            "## Requirements\n"
            f"{requirements}\n\n"
            "## Reported Deliverable\n"
            f"{deliverable}\n\n"
            "## Instructions\n"
            "Verify each success criterion by checking the actual state. Be "
            "thorough and skeptical. Report what you found.\n\n"
            "Respond with JSON:\n"
            '{"verdict": "pass" or "fail",\n'
            ' "checks": [{"criterion": "...", "verified": true/false, '
            '"evidence": "..."}],\n'
            ' "issues": ["list of genuine failures"]}'
        )

    def _parse_plan_review(self, content: str) -> ReviewResult:
        """Parse the plan review LLM response into a ReviewResult."""
        text = content.strip()

        # Strip code fences
        match = _JSON_BLOCK_RE.search(text)
        if match:
            text = match.group(1).strip()

        with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
            data = json.loads(text)
            if isinstance(data, dict):
                return ReviewResult(
                    passed=bool(data.get("passed", True)),
                    gaps=(
                        data["gaps"]
                        if isinstance(data.get("gaps"), list)
                        else []
                    ),
                    recommendations=(
                        data["recommendations"]
                        if isinstance(data.get("recommendations"), list)
                        else []
                    ),
                )

        # Unparseable -- pass through with the raw snippet
        logger.warning(
            "Could not parse plan review response, passing plan through",
        )
        return ReviewResult(
            passed=True,
            recommendations=[content[:200]],
        )
