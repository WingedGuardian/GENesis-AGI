"""Self-learning validation gate.

Interposes between procedure extraction and storage to adjust confidence
based on evidence quality, outcome history, and first-mover status.

Design principles:
- Pure filter: no side effects, no observation routing, no LLM calls.
- Fail-open: any exception → allow with default confidence (0.5).
- Outcome-class watermark: primary quality signal.
- v1 conservative: first-mover and regression are tag/flag only (no penalty).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import aiosqlite

from genesis.db.crud.watermarks import get_watermark, upsert_watermark

logger = logging.getLogger(__name__)

# --- Patterns ----------------------------------------------------------------

_IMPOSSIBILITY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(?:impossible|cannot be done|not possible|broken|doesn't work|won't work)\b",
        r"\b(?:deprecated|removed|discontinued|unsupported|no longer)\b",
        r"\b(?:fundamentally|inherently|structurally)\s+(?:broken|flawed|impossible)\b",
    ]
]

_EVIDENCE_MARKERS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"HTTP\s+\d{3}",
        r"Error:\s+\S+",
        r"Traceback\s+\(most recent",
        r"errno\s+\d+",
        r"exit\s+code\s+\d+",
        r"(?:API|endpoint)\s+returned",
        r"Permission denied|Access denied",
        r"ConnectionRefusedError|TimeoutError",
        r"Cloudflare|WAF|captcha|blocked by",
    ]
]

_OUTCOME_RANK = {"success": 3, "workaround_success": 2, "approach_failure": 1}

# --- Result type --------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Gate decision output."""

    allowed: bool
    adjusted_confidence: float
    flags: list[str] = field(default_factory=list)
    extraction_context: str = ""  # JSON blob for DB audit trail
    first_mover: bool = False


# --- Public entry point -------------------------------------------------------


async def validate_extraction(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    outcome: str,
    summary_text: str,
    session_tools_count: int,
) -> ValidationResult:
    """Run all validation checks and return a gate decision.

    Caller should:
    - If allowed=True: store with adjusted_confidence.
    - If allowed=False: discard extraction (evidence captured elsewhere).
    """
    try:
        return await _validate_inner(
            db,
            task_type=task_type,
            principle=principle,
            steps=steps,
            tools_used=tools_used,
            outcome=outcome,
            summary_text=summary_text,
            session_tools_count=session_tools_count,
        )
    except Exception:
        logger.warning("validation_gate: gate failed, allowing with default confidence",
                       exc_info=True)
        return ValidationResult(
            allowed=True,
            adjusted_confidence=0.5,
            flags=["gate_error_fail_open"],
            extraction_context=json.dumps({"error": "gate_exception"}),
        )


# --- Internal -----------------------------------------------------------------


async def _validate_inner(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    outcome: str,
    summary_text: str,
    session_tools_count: int,
) -> ValidationResult:
    flags: list[str] = []
    context: dict = {"outcome": outcome, "session_tools_count": session_tools_count}

    # --- Check 1: Evidence requirement ---
    evidence_mod = _check_evidence(principle, summary_text, flags)
    context["evidence_mod"] = evidence_mod

    if evidence_mod == 0.0:
        # Hard block: impossibility claim without evidence
        context["blocked"] = True
        return ValidationResult(
            allowed=False,
            adjusted_confidence=0.0,
            flags=flags,
            extraction_context=json.dumps(context),
        )

    # --- Check 2: Outcome-class watermark ---
    watermark = await get_watermark(db, task_type)
    watermark_mod = _check_watermark(watermark, outcome, flags)
    context["watermark_mod"] = watermark_mod
    context["watermark_existed"] = watermark is not None

    # --- Check 3: Regression detection (flag only in v1) ---
    regression_flag = False
    if watermark_mod < 1.0:
        regression_flag = await _check_regression(db, task_type, principle, flags)
    context["regression_flag"] = regression_flag

    # --- Check 4: First-mover (tag only in v1, no penalty) ---
    first_mover = watermark is None or watermark["total_sessions"] == 0
    if first_mover:
        flags.append("first_mover")
    context["first_mover"] = first_mover

    # --- Compose confidence ---
    # v1: only evidence_mod and watermark_mod affect confidence.
    # First-mover and regression are observational (tag/flag).
    adjusted = max(0.1, 0.5 * min(evidence_mod, watermark_mod))
    context["adjusted_confidence"] = adjusted

    # --- Update watermark ---
    await _update_watermark(db, task_type, outcome, watermark)

    logger.info(
        "validation_gate: decision=allowed task_type=%s outcome=%s conf=%.2f "
        "watermark=%s first_mover=%s flags=%s",
        task_type, outcome, adjusted,
        watermark is not None, first_mover, flags,
    )

    return ValidationResult(
        allowed=True,
        adjusted_confidence=adjusted,
        flags=flags,
        extraction_context=json.dumps(context),
        first_mover=first_mover,
    )


def _check_evidence(principle: str, summary_text: str, flags: list[str]) -> float:
    """Check 1: impossibility claims need evidence.

    Returns 0.0 (block) if impossibility without evidence, else 1.0.
    In v1 this is a penalty (conf=0.1) rather than hard block — see caller.
    """
    has_impossibility = any(p.search(principle) for p in _IMPOSSIBILITY_PATTERNS)
    if not has_impossibility:
        return 1.0

    flags.append("impossibility_claim")
    # Check both principle and summary for evidence
    combined = f"{principle} {summary_text}"
    has_evidence = any(m.search(combined) for m in _EVIDENCE_MARKERS)

    if has_evidence:
        flags.append("evidence_present")
        return 1.0

    flags.append("no_evidence_for_impossibility")
    return 0.0


def _check_watermark(
    watermark: dict | None, outcome: str, flags: list[str],
) -> float:
    """Check 2: outcome quality vs historical best.

    Returns modifier: 1.0 (no penalty), 0.5 (moderate), 0.3 (severe).
    """
    if watermark is None:
        return 1.0

    current_rank = _OUTCOME_RANK.get(outcome, 0)
    best_rank = _OUTCOME_RANK.get(watermark["best_outcome"], 0)

    if best_rank >= 3 and current_rank <= 1:
        # Task has been SUCCEEDED, this is a failure
        flags.append("regression_from_success")
        return 0.3
    elif best_rank >= 2 and current_rank <= 1:
        # Task had workaround success, this is pure failure
        flags.append("regression_from_workaround")
        return 0.5

    return 1.0


async def _check_regression(
    db: aiosqlite.Connection, task_type: str, principle: str, flags: list[str],
) -> bool:
    """Check 3: does new principle contradict validated procedures?

    v1: flag only, no confidence penalty. Returns True if contradiction detected.
    """
    try:
        cursor = await db.execute(
            "SELECT principle FROM procedural_memory "
            "WHERE task_type = ? AND success_count >= 2 AND quarantined = 0",
            (task_type,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            existing_principle = row[0]
            overlap = _jaccard_words(principle, existing_principle)
            if overlap > 0.3:
                flags.append("regression_contradicts_validated")
                return True
    except Exception:
        logger.debug("validation_gate: regression check failed", exc_info=True)
    return False


async def _update_watermark(
    db: aiosqlite.Connection, task_type: str, outcome: str, watermark: dict | None,
) -> None:
    """Update watermark after gate passes. Only ratchets outcome upward."""
    current_rank = _OUTCOME_RANK.get(outcome, 0)

    if watermark is None:
        await upsert_watermark(
            db,
            task_type=task_type,
            best_outcome=outcome,
            total_sessions=1,
            successful_sessions=1 if outcome == "success" else 0,
        )
    else:
        best_rank = _OUTCOME_RANK.get(watermark["best_outcome"], 0)
        new_best = outcome if current_rank > best_rank else watermark["best_outcome"]
        await upsert_watermark(
            db,
            task_type=task_type,
            best_outcome=new_best,
            total_sessions=watermark["total_sessions"] + 1,
            successful_sessions=(
                watermark["successful_sessions"] + (1 if outcome == "success" else 0)
            ),
        )


def _jaccard_words(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
