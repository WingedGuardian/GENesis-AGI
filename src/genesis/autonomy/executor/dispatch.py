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

_MAX_ARTIFACT_BYTES = 50_000  # 50 KB per artifact for deliverable enrichment

_TASK_STEP_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "identity" / "TASK_STEP.md"
)


def build_step_prompt(
    step: dict,
    prior_results: list[StepResult],
    workaround: str | None = None,
    resources: str | None = None,
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

    if resources:
        parts.extend([
            "## Resources for This Step",
            resources,
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


def _read_artifact(path_str: str) -> str:
    """Best-effort file read for deliverable enrichment. Never raises."""
    path = Path(path_str).expanduser()
    header = f"### Artifact: `{path_str}`"
    if not path.exists():
        return f"{header}\n**ERROR: File does not exist**"
    if not path.is_file():
        return f"{header}\n**Skipped: not a regular file**"
    try:
        size = path.stat().st_size
    except OSError:
        return f"{header}\n**Skipped: cannot stat file**"
    if size > _MAX_ARTIFACT_BYTES:
        try:
            raw = path.read_bytes()[:_MAX_ARTIFACT_BYTES]
            content = raw.decode("utf-8", errors="replace")
        except OSError:
            return f"{header}\n**Skipped: read error**"
        return (
            f"{header}\n**Truncated** ({size:,} bytes)\n```\n"
            f"{content}\n```"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return f"{header}\n**Skipped: binary or unreadable**"
    return f"{header}\n```\n{content}\n```"


def synthesize_deliverable(step_results: list[StepResult]) -> str:
    """Combine completed step results into a deliverable string.

    Includes artifact file contents (best-effort) so reviewers can
    evaluate actual output, not just narrative text.
    """
    parts = []
    for r in step_results:
        if r.status == "completed":
            parts.append(f"## Step {r.idx}\n{r.result}")
            for path_str in r.artifacts:
                parts.append(_read_artifact(path_str))
    return "\n\n".join(parts) if parts else ""


def dominant_step_type(steps: list[dict]) -> str:
    """Return the most common step type."""
    types = [s.get("type", "code") for s in steps]
    if not types:
        return "code"
    return max(set(types), key=types.count)


def create_fixup_step(
    verify,
    idx: int,
    plan_content: str = "",
) -> dict:
    """Create a fixup step from review feedback.

    *plan_content*: the original task plan, included so the fixup CC
    session can reference the success criteria and requirements.
    """
    _FEEDBACK_LIMIT = 2000

    feedback_parts = []
    if verify.fresh_eyes_feedback:
        feedback_parts.append(
            f"Fresh-eyes feedback:\n{verify.fresh_eyes_feedback[:_FEEDBACK_LIMIT]}"
        )
    if verify.adversarial_feedback:
        feedback_parts.append(
            f"Adversarial feedback:\n{verify.adversarial_feedback[:_FEEDBACK_LIMIT]}"
        )
    if verify.programmatic_issues:
        feedback_parts.append(
            "Programmatic issues: " + "; ".join(verify.programmatic_issues)
        )

    description_parts = [
        "Address review feedback and fix identified issues:",
        "",
        *feedback_parts,
    ]

    if plan_content:
        description_parts.extend([
            "",
            "## Original Plan (for reference — especially success criteria)",
            plan_content[:4000],
        ])

    return {
        "idx": idx,
        "type": "code",
        "description": "\n".join(description_parts),
        "required_tools": [],
        "complexity": "medium",
        "dependencies": [],
    }
