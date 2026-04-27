"""Adversarial quality gate -- dual LLM review for task deliverables.

Implements the quality gate from the design doc:
1. Programmatic checks (basic validation)
2. Fresh-eyes review (call site 17, cross-vendor)
3. Adversarial counterargument (call site 20, cross-vendor)

If programmatic checks fail, LLM review is skipped entirely.
If cross-vendor routing fails after retries, review is skipped with
a warning (Amendment #5).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
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
        from genesis.cc.types import CCInvocation, CCModel, EffortLevel

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.OPUS,
            effort=EffortLevel.HIGH,
            timeout_s=300,
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
    ) -> VerifyResult:
        """Post-execution dual-gate verification.

        Gate 1: Programmatic checks (basic validation).
        Gate 2: Fresh-eyes review (call site 17, cross-vendor).
        Gate 3: Adversarial review (call site 20, cross-vendor).

        If programmatic checks fail, LLM gates are skipped.
        If both LLM routes fail, delivers with a warning (Amendment #5).
        """
        # Gate 1 -- programmatic checks
        issues = self._programmatic_checks(deliverable, task_type)
        if issues:
            return VerifyResult(
                passed=False,
                programmatic_issues=issues,
                iteration=iteration,
            )

        # Gate 2 -- fresh-eyes review
        fresh_eyes = await self._llm_review(
            _CALL_SITE_FRESH, deliverable, requirements,
        )

        # Gate 3 -- adversarial review
        adversarial = await self._llm_review(
            _CALL_SITE_ADVERSARIAL, deliverable, requirements,
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
            "Review this task plan for completeness, clarity, and "
            "feasibility.\n\n"
            "## Task Description\n"
            f"{task_description}\n\n"
            "## Plan\n"
            f"{plan_content}\n\n"
            "## Instructions\n"
            "Identify any gaps, missing details, unclear steps, or "
            "potential issues.\n\n"
            'Respond with a JSON object:\n'
            '{"passed": true/false, "gaps": ["list of gaps"], '
            '"recommendations": ["list of recommendations"]}'
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
