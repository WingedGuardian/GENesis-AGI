"""Step 2.2 — LLM-backed request-delivery delta assessor."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any, Protocol

from genesis.learning.types import (
    DeltaClassification,
    DiscoveryAttribution,
    InteractionSummary,
    RequestDeliveryDelta,
    ScopeEvolution,
)


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


# 32_delta_assessment — daily delta classification between cognitive-state snapshots.
_CALL_SITE = "32_delta_assessment"


class DeltaAssessor:
    """Assess the delta between what was requested and what was delivered."""

    def __init__(self, router: _Router) -> None:
        self._router = router

    async def assess(self, summary: InteractionSummary) -> RequestDeliveryDelta:
        prompt = self._build_prompt(summary)
        messages = [{"role": "user", "content": prompt}]
        result = await self._router.route_call(_CALL_SITE, messages)

        if not result.success or not result.content:
            return self._fallback()

        return self._parse_response(result.content)

    def _build_prompt(self, summary: InteractionSummary) -> str:
        return "\n".join([
            "You are a delta assessor for an AI agent's retrospective learning system.",
            "Compare what the user requested with what was delivered.",
            "",
            "## Delta Classifications",
            "- exact_match: delivery matched the request precisely",
            "- acceptable_shortfall: minor gaps but acceptable outcome",
            "- over_delivery: delivered more than requested",
            "- misinterpretation: fundamentally misunderstood the request",
            "",
            "## Discovery Attributions (select ALL that apply)",
            "- external_limitation: external service/resource prevented full delivery",
            "- user_model_gap: Genesis misunderstood the user's preferences or context",
            "- genesis_capability: Genesis lacked a needed capability",
            "- genesis_interpretation: Genesis misinterpreted the request",
            "- scope_underspecified: the request was ambiguous or underspecified",
            "- user_revised_scope: the user changed scope during the interaction",
            "",
            "## Interaction",
            f"Session: {summary.session_id}",
            f"User: {summary.user_text}",
            f"Response: {summary.response_text}",
            f"Tools used: {', '.join(summary.tool_calls) or 'none'}",
            "",
            "Respond with JSON:",
            '{',
            '  "classification": "<delta_class>",',
            '  "attributions": ["<attribution1>", ...],',
            '  "evidence": "<brief explanation>",',
            '  "scope_evolution": {',
            '    "original_request": "<what was asked>",',
            '    "final_delivery": "<what was delivered>",',
            '    "scope_communicated": true/false',
            '  } or null',
            '}',
        ])

    def _parse_response(self, content: str) -> RequestDeliveryDelta:
        data: dict[str, Any] | None = None

        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(content.strip())

        if data is None:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    data = json.loads(json_match.group())

        if data is None:
            return self._fallback()

        try:
            classification = DeltaClassification(
                str(data.get("classification", "exact_match")).lower().strip()
            )
        except ValueError:
            classification = DeltaClassification.EXACT_MATCH

        attributions: list[DiscoveryAttribution] = []
        for attr_str in data.get("attributions", []):
            with contextlib.suppress(ValueError):
                attributions.append(
                    DiscoveryAttribution(str(attr_str).lower().strip())
                )

        evidence = str(data.get("evidence", ""))

        scope_evo = None
        scope_data = data.get("scope_evolution")
        if isinstance(scope_data, dict):
            scope_evo = ScopeEvolution(
                original_request=str(scope_data.get("original_request", "")),
                final_delivery=str(scope_data.get("final_delivery", "")),
                scope_communicated=bool(scope_data.get("scope_communicated", False)),
            )

        return RequestDeliveryDelta(
            classification=classification,
            attributions=attributions,
            scope_evolution=scope_evo,
            evidence=evidence,
        )

    def _fallback(self) -> RequestDeliveryDelta:
        return RequestDeliveryDelta(
            classification=DeltaClassification.EXACT_MATCH,
            attributions=[],
            scope_evolution=None,
            evidence="",
        )
