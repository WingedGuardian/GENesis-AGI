"""Step 2.1 — LLM-backed outcome classifier."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any, Protocol

from genesis.learning.types import InteractionSummary, OutcomeClass


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
            "Classify the following interaction into exactly one outcome class.",
            "",
            "## Outcome Classes",
            "- success: task completed as requested",
            "- approach_failure: wrong approach used, could have done better",
            "- capability_gap: Genesis lacks the ability to complete the task."
            " REQUIRES exhaustion evidence — must have tried alternatives and failed.",
            "- external_blocker: an external system prevented completion."
            " REQUIRES exhaustion evidence — must have tried alternatives and failed.",
            "- workaround_success: primary approach failed but an alternative worked",
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
            'Respond with JSON: {"outcome": "<class_name>", "rationale": "<brief reason>"}',
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
            with contextlib.suppress(ValueError):
                return OutcomeClass(outcome_str)

        # Parse failed: response was non-empty but unusable. This is an error
        # state, not a success — returning SUCCESS here previously caused silent
        # false-positive autonomy updates and procedure-extraction skips.
        return OutcomeClass.CLASSIFICATION_FAILED
