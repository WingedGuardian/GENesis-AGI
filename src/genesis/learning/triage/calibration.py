"""Daily triage calibration cycle — reviews recent triage decisions and updates rules."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from genesis.learning.events import LEARNING_EVENTS
from genesis.learning.types import CalibrationRules
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

_DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "identity" / "TRIAGE_CALIBRATION.md"

_CALIBRATION_PROMPT = """\
You are reviewing recent triage classification decisions to improve calibration.

Here are recent triage-related observations from the last 24 hours:

{observations}

Current calibration file:
{current_calibration}

Your task:
1. Review the observations for under-classification (depth too low) and over-classification (depth too high).
2. Generate updated few-shot examples and calibration rules.
3. You MUST produce at least 5 examples covering depths 0 through 4.

Respond with ONLY a JSON object (no markdown fencing) with this structure:
{{
  "examples": [
    {{"scenario": "...", "depth": 0, "rationale": "..."}},
    ...
  ],
  "rules": ["rule text", ...],
  "source_model": "your model name or identifier"
}}
"""

_MIN_EXAMPLES = 5
_REQUIRED_DEPTHS = {0, 1, 2, 3, 4}


class TriageCalibrator:
    """Runs the daily triage calibration cycle."""

    def __init__(
        self,
        router,
        db,
        memory_store=None,
        calibration_path: Path | None = None,
        event_bus=None,
    ):
        self._router = router
        self._db = db
        self._memory_store = memory_store
        self._calibration_path = calibration_path or _DEFAULT_CALIBRATION_PATH
        self._event_bus = event_bus

    async def run_daily_calibration(self) -> CalibrationRules | None:
        """Run daily calibration cycle. Returns CalibrationRules or None on failure."""
        # 1. Sample recent triage observations
        observations = await self._query_recent_observations()
        if not observations:
            logger.info("No recent triage observations found, skipping calibration")
            if self._event_bus:
                await self._event_bus.emit(
                    subsystem=Subsystem.LEARNING,
                    severity=Severity.INFO,
                    event_type=LEARNING_EVENTS["CALIBRATION_FAILED"],
                    message="No recent triage observations",
                )
            return None

        # 2. Read current calibration file
        current_calibration = ""
        if self._calibration_path.exists():
            current_calibration = self._calibration_path.read_text()

        # 3. Build prompt and call LLM
        obs_text = "\n".join(
            f"- [{row.get('created_at', '?')}] {row.get('content', '')}"
            for row in observations
        )
        prompt = _CALIBRATION_PROMPT.format(
            observations=obs_text, current_calibration=current_calibration
        )

        result = await self._router.route_call(
            "30_triage_calibration",
            [{"role": "user", "content": prompt}],
        )

        if not result.success or not result.content:
            logger.warning("Calibration LLM call failed")
            if self._event_bus:
                await self._event_bus.emit(
                    subsystem=Subsystem.LEARNING,
                    severity=Severity.WARNING,
                    event_type=LEARNING_EVENTS["CALIBRATION_FAILED"],
                    message="LLM call failed",
                )
            return None

        # 4. Parse and validate
        rules = self._parse_and_validate(result.content)
        if rules is None:
            logger.warning("Calibration output failed validation")
            if self._event_bus:
                await self._event_bus.emit(
                    subsystem=Subsystem.LEARNING,
                    severity=Severity.WARNING,
                    event_type=LEARNING_EVENTS["CALIBRATION_FAILED"],
                    message="Validation failed",
                )
            return None

        # 5. Write atomically
        self._write_calibration(rules)

        if self._event_bus:
            await self._event_bus.emit(
                subsystem=Subsystem.LEARNING,
                severity=Severity.INFO,
                event_type=LEARNING_EVENTS["CALIBRATION_COMPLETED"],
                message=f"Calibration updated with {len(rules.examples)} examples",
                example_count=len(rules.examples),
                rule_count=len(rules.rules),
            )

        return rules

    async def _query_recent_observations(self) -> list[dict]:
        """Query observations table for recent triage-related entries."""
        cursor = await self._db.execute(
            "SELECT content, created_at FROM observations "
            "WHERE source = 'retrospective' AND type LIKE '%triage%' "
            "AND created_at >= datetime('now', '-24 hours') "
            "ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
        return [{"content": row[0], "created_at": row[1]} for row in rows]

    def _parse_and_validate(self, content: str) -> CalibrationRules | None:
        """Parse LLM output and validate minimum requirements."""
        data = None

        # 1. Try direct json.loads first.
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(content.strip())

        # 2. Greedy regex fallback — handles markdown ```json fences.
        if data is None:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    data = json.loads(json_match.group())

        if data is None:
            logger.warning("Calibration output is not valid JSON")
            return None

        examples = data.get("examples", [])
        rules = data.get("rules", [])
        source_model = data.get("source_model", "unknown")

        if len(examples) < _MIN_EXAMPLES:
            logger.warning("Too few examples: %d < %d", len(examples), _MIN_EXAMPLES)
            return None

        depths_covered = {ex.get("depth") for ex in examples}
        if not _REQUIRED_DEPTHS.issubset(depths_covered):
            missing = _REQUIRED_DEPTHS - depths_covered
            logger.warning("Missing depth coverage: %s", missing)
            return None

        return CalibrationRules(
            examples=examples,
            rules=rules,
            generated_at=datetime.now(UTC),
            source_model=source_model,
        )

    def _write_calibration(self, rules: CalibrationRules) -> None:
        """Write calibration file atomically (write .tmp, rename)."""
        tmp_path = self._calibration_path.with_suffix(".md.tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "---",
            'version: "1.0"',
            "description: >",
            "  Few-shot calibration examples for the triage classifier.",
            f"  Generated at {rules.generated_at.isoformat()} by {rules.source_model}.",
            "---",
            "",
            "## Few-Shot Examples",
            "",
            "| # | Scenario | Depth | Rationale |",
            "|---|----------|-------|-----------|",
        ]
        for i, ex in enumerate(rules.examples, 1):
            lines.append(
                f"| {i} | {ex.get('scenario', '')} | {ex.get('depth', '?')} | {ex.get('rationale', '')} |"
            )

        lines.extend([
            "",
            "## Calibration Rules",
            "",
        ])
        for rule in rules.rules:
            lines.append(f"- {rule}")
        lines.append("")

        tmp_path.write_text("\n".join(lines))
        tmp_path.rename(self._calibration_path)
