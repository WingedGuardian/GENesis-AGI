"""OutputParser — validates LLM responses against depth-specific schemas."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from genesis.perception.types import LightOutput, LLMResponse, MicroOutput, UserModelDelta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseResult:
    """Result of parsing an LLM response."""

    success: bool
    output: MicroOutput | LightOutput | None = None
    needs_retry: bool = False
    retry_prompt: str | None = None
    error: str | None = None


class OutputParser:
    """Validates LLM responses against output contracts."""

    def parse(self, response: LLMResponse, depth: str) -> ParseResult:
        text = self._extract_json_text(response.text)

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your response was not valid JSON. Error: {e}. "
                    "Please respond with ONLY a JSON object matching the schema.",
                ),
                error=str(e),
            )

        d = depth.lower()
        if d == "micro":
            return self._validate_micro(data)
        if d == "light":
            return self._validate_light(data)

        return ParseResult(success=False, error=f"Unsupported depth: {depth}")

    def _extract_json_text(self, text: str) -> str:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()

    def _validate_micro(self, data: dict) -> ParseResult:
        errors = []
        if not isinstance(data.get("tags"), list):
            errors.append("'tags' must be a list")
        if "salience" not in data:
            errors.append("'salience' is required")
        elif not isinstance(data["salience"], (int, float)):
            errors.append("'salience' must be a number")
        elif not (0.0 <= data["salience"] <= 1.0):
            errors.append("'salience' must be between 0.0 and 1.0")
        if "anomaly" not in data:
            errors.append("'anomaly' is required")
        elif not isinstance(data["anomaly"], bool):
            errors.append("'anomaly' must be a boolean")
        if "summary" not in data:
            errors.append("'summary' is required")
        if "signals_examined" not in data:
            errors.append("'signals_examined' is required")

        if errors:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your JSON output had validation errors: {'; '.join(errors)}. "
                    "Please fix and respond with the corrected JSON.",
                ),
                error="; ".join(errors),
            )

        return ParseResult(
            success=True,
            output=MicroOutput(
                tags=data["tags"],
                salience=float(data["salience"]),
                anomaly=bool(data["anomaly"]),
                summary=str(data["summary"]),
                signals_examined=int(data["signals_examined"]),
            ),
        )

    def _validate_light(self, data: dict) -> ParseResult:
        errors = []
        if "assessment" not in data:
            errors.append("'assessment' is required")
        if not isinstance(data.get("patterns"), list):
            errors.append("'patterns' must be a list")
        if not isinstance(data.get("user_model_updates"), list):
            errors.append("'user_model_updates' must be a list")
        if not isinstance(data.get("recommendations"), list):
            errors.append("'recommendations' must be a list")
        if "confidence" not in data:
            errors.append("'confidence' is required")
        elif not isinstance(data["confidence"], (int, float)):
            errors.append("'confidence' must be a number")
        elif not (0.0 <= data["confidence"] <= 1.0):
            errors.append("'confidence' must be between 0.0 and 1.0")
        if "focus_area" not in data:
            errors.append("'focus_area' is required")

        if errors:
            return ParseResult(
                success=False,
                needs_retry=True,
                retry_prompt=self._retry_prompt(
                    f"Your JSON output had validation errors: {'; '.join(errors)}. "
                    "Please fix and respond with the corrected JSON.",
                ),
                error="; ".join(errors),
            )

        deltas = []
        for item in data.get("user_model_updates", []):
            if isinstance(item, dict):
                deltas.append(UserModelDelta(
                    field=str(item.get("field", "")),
                    value=str(item.get("value", "")),
                    evidence=str(item.get("evidence", "")),
                    confidence=float(item.get("confidence", 0.0)),
                ))

        # Enforce output caps — prompt asks for rank order (most important first),
        # so keeping the first N preserves the highest-priority items.
        _MAX_RECOMMENDATIONS = 3
        _MAX_PATTERNS = 3
        _MAX_SURPLUS = 3

        patterns = [str(p) for p in data["patterns"][:_MAX_PATTERNS]]
        recommendations = data["recommendations"][:_MAX_RECOMMENDATIONS]
        surplus = [str(c) for c in data.get("surplus_candidates", []) if c][:_MAX_SURPLUS]

        if len(data["recommendations"]) > _MAX_RECOMMENDATIONS:
            logger.info(
                "Light reflection capped: %d→%d recommendations",
                len(data["recommendations"]), _MAX_RECOMMENDATIONS,
            )
        if len(data["patterns"]) > _MAX_PATTERNS:
            logger.info(
                "Light reflection capped: %d→%d patterns",
                len(data["patterns"]), _MAX_PATTERNS,
            )

        return ParseResult(
            success=True,
            output=LightOutput(
                assessment=str(data["assessment"]),
                patterns=patterns,
                user_model_updates=deltas,
                recommendations=recommendations,
                confidence=float(data["confidence"]),
                focus_area=str(data["focus_area"]),
                escalate_to_deep=bool(data.get("escalate_to_deep", False)),
                escalation_reason=data.get("escalation_reason"),
                surplus_candidates=surplus,
            ),
        )

    def _retry_prompt(self, error_message: str) -> str:
        return (
            f"Your previous response had an error:\n{error_message}\n\n"
            "Please try again with a corrected JSON response."
        )
