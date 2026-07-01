"""Step 2.1 — LLM-backed outcome classifier."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any, Protocol

from genesis.learning.types import InteractionSummary, OutcomeClass

logger = logging.getLogger(__name__)


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


# 31_outcome_classification — per-outcome success/partial/failure classification.
# Feeds the learning pipeline and the executor retrospective. See _call_site_meta.py.
_CALL_SITE = "31_outcome_classification"


class OutcomeClassifier:
    """Classify interaction outcomes into one of 5 classes."""

    def __init__(self, router: _Router) -> None:
        self._router = router

    async def classify(
        self, summary: InteractionSummary, trace_context: str = ""
    ) -> OutcomeClass:
        prompt = self._build_prompt(summary, trace_context)
        messages = [{"role": "user", "content": prompt}]
        result = await self._router.route_call(_CALL_SITE, messages)

        if not result.success or not result.content:
            return OutcomeClass.CLASSIFICATION_FAILED

        return self._parse_response(result.content)

    def _build_prompt(
        self, summary: InteractionSummary, trace_context: str
    ) -> str:
        parts = [
            "You are an outcome classifier for an AI agent's retrospective learning system.",
            "The agent is named Genesis. Classify the following interaction into exactly one"
            " outcome class.",
            "",
            "## Outcome Classes",
            "- success: Genesis did everything it ATTEMPTED this interaction, OR the turn contained",
            "  no concrete task for Genesis to attempt (the user only shared information, expressed",
            "  a future intent, or asked something open-ended) and Genesis responded appropriately",
            "  (including by proposing options or asking a clarifying question before acting).",
            "  NOTE: if the turn DID include a concrete task Genesis attempted, judge by that task —",
            "  a status update elsewhere in the same message does not make the turn a 'success'.",
            "- approach_failure: GENESIS actually ATTEMPTED a concrete task THIS interaction and its",
            "  OWN approach was suboptimal or left the attempt partially done. About GENESIS's",
            "  execution of something it TRIED — never the real-world fate of the user's external",
            "  projects, and never a task Genesis has not yet started.",
            "- capability_gap: Genesis lacks the ability to complete the task."
            " REQUIRES exhaustion evidence — must have tried alternatives and failed.",
            "- external_blocker: an external system prevented completion."
            " REQUIRES exhaustion evidence — must have tried alternatives and failed.",
            "- workaround_success: primary approach failed but an alternative worked",
            "",
            "## What is a GOAL, and when is it FAILED (scope carefully)",
            "A 'goal' is a concrete task the user asked GENESIS to DO or PRODUCE in THIS interaction",
            "(fetch X, fix Y, draft Z, deploy W, answer a question) AND that Genesis then ATTEMPTED.",
            "A goal is FAILED only if Genesis attempted it this turn and fell short. If Genesis did",
            "not attempt it this turn, it is NOT a failed goal — leave it out of goals_failed.",
            "",
            "These are NEVER goals and NEVER belong in goals_failed:",
            "- Real-world statuses the user narrates about their OWN external projects, jobs, events,",
            "  or deadlines ('the offer fell through', 'I didn't attend the conference', 'the paper",
            "  didn't get submitted'). Their real-world outcome is not Genesis's failure.",
            "- Forward-looking intentions ('we should continue on X', 'let's keep going on Y', 'we",
            "  should do Z sometime', 'it's never too late'). Nothing has been attempted yet.",
            "",
            "If Genesis responds to an informational or open-ended message by summarizing the",
            "situation, proposing options, or asking a clarifying question BEFORE acting, that is",
            "CORRECT behavior → outcome 'success', goals_failed empty. Gathering context or awaiting",
            "the user's go-ahead is not a failure.",
            "",
            "## Two worked examples (note goals_failed)",
            "EX-A (status + forward intent, Genesis proposes and asks):",
            "  User: 'The offer fell through and I didn't attend the conference, but we should keep",
            "  going on the report — it's never too late.'",
            "  Genesis: 'Here's where the report stands... want me to pull the latest numbers and do",
            "  a revision pass?'",
            '  → {"goals_identified": [], "goals_achieved": [], "goals_failed": [],'
            ' "outcome": "success", "rationale": "User shared statuses + a forward intent; Genesis'
            ' summarized and proposed next steps, awaiting go-ahead. Nothing was attempted-and-failed."}',
            "EX-B (concrete task, Genesis attempted, fell short):",
            "  User: 'Fetch these 4 URLs and summarize each.'",
            "  Genesis: 'I summarized 3 of 4; the 4th returned HTTP 403.'",
            '  → {"goals_identified": ["fetch+summarize 4 URLs"], "goals_achieved": ["3 of 4"],'
            ' "goals_failed": ["fetch+summarize URL 4"], "outcome": "approach_failure", "rationale":'
            ' "Genesis attempted the task and left 1 of 4 undone."}',
            "",
            "## Goal Validation",
            "Before classifying, explicitly identify:",
            "1. What concrete tasks did the user ask GENESIS to do THIS turn, that Genesis then",
            "   ATTEMPTED? (Exclude external statuses and forward intents. If none, use [].)",
            "2. Of those attempted tasks, which were achieved and which fell short?",
            "3. If an ATTEMPTED task fell short → 'approach_failure'/'workaround_success', never",
            "   'success'. If goals_identified is empty (no task attempted), the outcome is",
            "   'success', not a failure.",
            "",
        ]

        if trace_context:
            parts.append("## Trace Context")
            parts.append(trace_context)
            parts.append("")

        parts.extend([
            "## Interaction",
            f"Session: {summary.session_id}",
            f"Channel: {summary.channel}",
            f"Tools used: {', '.join(summary.tool_calls) or 'none'}",
            f"User: {summary.user_text}",
            f"Response: {summary.response_text}",
            "",
            "Respond with JSON:",
            '{"goals_identified": ["goal1", "goal2"],'
            ' "goals_achieved": ["goal1"],'
            ' "goals_failed": ["goal2"],'
            ' "outcome": "<class_name>",'
            ' "rationale": "<brief reason>"}',
        ])

        return "\n".join(parts)

    def _parse_response(self, content: str) -> OutcomeClass:
        data: dict[str, Any] | None = None

        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(content.strip())

        if data is None:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    data = json.loads(json_match.group())

        if data is not None:
            outcome_str = str(data.get("outcome", "")).lower().strip()

            # Hard gate: if the LLM identified failed goals but still
            # classified as "success", override.  Partial completion is
            # approach_failure, never success.
            goals_failed = data.get("goals_failed")
            if goals_failed and outcome_str == "success":
                logger.warning(
                    "Outcome hard gate: %d goal(s) failed but classified "
                    "as success — overriding to approach_failure",
                    len(goals_failed),
                )
                return OutcomeClass.APPROACH_FAILURE

            with contextlib.suppress(ValueError):
                return OutcomeClass(outcome_str)

        # Parse failed: response was non-empty but unusable. This is an error
        # state, not a success — returning SUCCESS here previously caused silent
        # false-positive autonomy updates and procedure-extraction skips.
        return OutcomeClass.CLASSIFICATION_FAILED
