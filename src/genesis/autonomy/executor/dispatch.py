"""Step dispatch helpers for the task executor.

Extracted from engine.py to keep file size under 600 LOC.
Contains prompt building, output parsing, deliverable synthesis,
and fixup step creation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path

from genesis.autonomy.executor.types import StepResult

logger = logging.getLogger(__name__)

# CC output JSON extraction (same pattern as perception/parser.py)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)

_TASK_STEP_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "identity" / "TASK_STEP.md"
)


def build_step_prompt(
    step: dict,
    prior_results: list[StepResult],
    workaround: str | None = None,
) -> str:
    """Build the step prompt from TASK_STEP.md template."""
    template = ""
    try:
        template = _TASK_STEP_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error(
            "Failed to load step template from %s",
            _TASK_STEP_PROMPT_PATH, exc_info=True,
        )

    prior_summary = "\n".join(
        f"Step {r.idx}: {r.status} -- {r.result[:200]}"
        for r in prior_results
    )

    parts: list[str] = []
    if template:
        parts.append(template)
        parts.append("")

    parts.extend([
        f"## Step {step['idx']}: {step.get('description', '')}",
        f"Type: {step.get('type', 'code')}",
        f"Complexity: {step.get('complexity', 'medium')}",
        "",
    ])

    if prior_summary:
        parts.extend(["## Prior Step Results", prior_summary, ""])

    if workaround:
        parts.extend([
            "## Workaround Context",
            "A previous attempt at this step failed. "
            "Use the following approach instead:",
            workaround,
            "",
        ])

    return "\n".join(parts)


def parse_step_output(text: str) -> dict:
    """Extract JSON status from CC output.

    Follows the codebase canonical pattern: try markdown backtick
    extraction first, then search for last JSON block, then
    fall back to defaults.
    """
    if not text:
        return {"status": "completed", "result": ""}

    # Try backtick extraction (canonical pattern)
    match = _JSON_BLOCK_RE.search(text)
    if match:
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict):
                return data

    # Search from end for a JSON object
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                data = json.loads(line)
                if isinstance(data, dict):
                    return data

    return {"status": "completed", "result": text[:500]}


def synthesize_deliverable(step_results: list[StepResult]) -> str:
    """Combine completed step results into a deliverable string."""
    parts = []
    for r in step_results:
        if r.status == "completed":
            parts.append(f"## Step {r.idx}\n{r.result}")
    return "\n\n".join(parts) if parts else ""


def dominant_step_type(steps: list[dict]) -> str:
    """Return the most common step type."""
    types = [s.get("type", "code") for s in steps]
    if not types:
        return "code"
    return max(set(types), key=types.count)


def create_fixup_step(verify, idx: int) -> dict:
    """Create a fixup step from review feedback."""
    feedback_parts = []
    if verify.fresh_eyes_feedback:
        feedback_parts.append(
            f"Fresh-eyes feedback: {verify.fresh_eyes_feedback[:500]}"
        )
    if verify.adversarial_feedback:
        feedback_parts.append(
            f"Adversarial feedback: {verify.adversarial_feedback[:500]}"
        )
    if verify.programmatic_issues:
        feedback_parts.append(
            "Programmatic issues: " + "; ".join(verify.programmatic_issues)
        )

    return {
        "idx": idx,
        "type": "code",
        "description": (
            "Address review feedback and fix identified issues:\n"
            + "\n".join(feedback_parts)
        ),
        "required_tools": [],
        "complexity": "medium",
        "dependencies": [],
    }
