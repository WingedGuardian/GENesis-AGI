"""Step 1.8 — LLM-backed triage classifier."""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from genesis.learning.types import InteractionSummary, TriageDepth, TriageResult

_DEFAULT_CALIBRATION = (
    Path(__file__).resolve().parents[2] / "identity" / "TRIAGE_CALIBRATION.md"
)

_FALLBACK_DEPTH = TriageDepth.QUICK_NOTE


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


class TriageClassifier:
    """Classify interactions into triage depths via an LLM call."""

    def __init__(
        self,
        router: _Router,
        calibration_path: Path | None = None,
    ) -> None:
        self._router = router
        self._calibration_path = calibration_path or _DEFAULT_CALIBRATION
        self._calibration_text: str | None = None
        self._calibration_mtime: float = 0.0

    # ── public ────────────────────────────────────────────────────────────

    async def classify(self, summary: InteractionSummary) -> TriageResult:
        calibration = self._load_calibration()
        prompt = self._build_prompt(summary, calibration)
        messages = [{"role": "user", "content": prompt}]

        # 29_retrospective_triage — THE LIVE triage classifier (per-outcome depth).
        # NOT 2_triage (removed 2026-05-10), 30_triage_calibration (calibration rules),
        # or email_triage (outreach domain). See _call_site_meta.py for the family map.
        result = await self._router.route_call("29_retrospective_triage", messages)

        if not result.success or not result.content:
            return TriageResult(
                depth=_FALLBACK_DEPTH,
                rationale="router call failed",
                skipped_by_prefilter=False,
            )

        return self._parse_response(result.content)

    async def classify_batch(
        self, summaries: list[InteractionSummary]
    ) -> list[TriageResult]:
        """Classify a list of summaries sequentially."""
        return [await self.classify(s) for s in summaries]

    # ── internal ──────────────────────────────────────────────────────────

    def _load_calibration(self) -> str:
        path = self._calibration_path
        if not path.exists():
            return ""
        mtime = os.path.getmtime(path)
        if self._calibration_text is None or mtime != self._calibration_mtime:
            self._calibration_text = path.read_text()
            self._calibration_mtime = mtime
        return self._calibration_text

    def _build_prompt(self, summary: InteractionSummary, calibration: str) -> str:
        parts = [
            "You are a triage classifier for an AI agent's retrospective learning system.",
            "Classify the following interaction into a depth level (0-4):",
            "",
            "Depth levels:",
            "  0 = SKIP — nothing to learn",
            "  1 = QUICK_NOTE — minor observation only",
            "  2 = WORTH_THINKING — deserves light analysis",
            "  3 = FULL_ANALYSIS — full outcome + delta analysis",
            "  4 = FULL_PLUS_WORKAROUND — complex, needs workaround extraction",
            "",
        ]

        if calibration:
            parts.append("## Calibration Examples")
            parts.append(calibration)
            parts.append("")

        parts.extend([
            "## Interaction",
            f"Session: {summary.session_id}",
            f"Channel: {summary.channel}",
            f"Tokens: {summary.token_count}",
            f"Tools used: {', '.join(summary.tool_calls) or 'none'}",
            f"User: {summary.user_text}",
            f"Response: {summary.response_text}",
            "",
            'Respond with JSON: {"depth": <int 0-4>, "rationale": "<brief reason>"}',
        ])

        return "\n".join(parts)

    def _parse_response(self, content: str) -> TriageResult:
        # Try direct json.loads first, then greedy regex fallback.
        data: dict[str, Any] | None = None

        # 1. Try parsing the full content as JSON.
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(content.strip())

        # 2. Greedy regex extraction (handles rationale containing `}`).
        if data is None:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    data = json.loads(json_match.group())

        # 3. Validate and return.
        if data is not None:
            try:
                depth_val = int(data["depth"])
                if 0 <= depth_val <= 4:
                    return TriageResult(
                        depth=TriageDepth(depth_val),
                        rationale=str(data.get("rationale", "")),
                        skipped_by_prefilter=False,
                    )
            except (KeyError, ValueError, TypeError):
                pass

        return TriageResult(
            depth=_FALLBACK_DEPTH,
            rationale="parse failure — defaulting to QUICK_NOTE",
            skipped_by_prefilter=False,
        )
