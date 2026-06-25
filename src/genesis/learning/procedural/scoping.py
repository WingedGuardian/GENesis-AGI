"""Procedure scoping gate — keep behavioral DIRECTIVES out of the procedure store.

The procedure store is for reusable TASK procedures: how a *specific external
technical system* works (IMAP consumers, Docker layer caching, MCP wiring, an
OAuth flow…). Behavioral DIRECTIVES — how the agent should work *in general*
(state confidence, do due diligence, investigate before planning, when to ask
the user) — belong in the standing instructions (CLAUDE.md), NOT here. The
agent re-derives the same directive from almost every session, so directives are
the dominant source of near-duplicate procedures (e.g. ~40 paraphrases of
"do honest confidence + due diligence before planning").

This module classifies an already-extracted procedure and reports whether it is
really a behavioral directive. The classifier is one router LLM call using the
same call site as extraction (``38_procedure_extraction``).

**Safety contract (validated 2026-06-24 on a 40-item hand-labeled, held-out
sample — 100% directive recall / 0% procedure false-positive):** the classifier
ERRS TOWARD ``task_procedure``. A directive slipping through is harmless (a low
-value procedure, no worse than today). Suppressing a real procedure is NOT — it
loses institutional memory. Therefore every failure mode here (call error,
unsuccessful result, unparseable output, unknown label) FAILS OPEN to "keep".
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Reuse the extraction call site so dedup-classifier cost is attributed with the
# rest of the procedure-extraction spend (no new routing config / test churn).
_CALL_SITE = "38_procedure_extraction"

PROCEDURE_TYPE_TASK = "task_procedure"
PROCEDURE_TYPE_DIRECTIVE = "behavioral_directive"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# The validated v3 discriminator. Judge by WHAT THE LESSON IS ABOUT (the agent's
# general working process vs a specific external technical system), not by how
# the principle is phrased, and err toward task_procedure when unsure.
_CLASSIFIER_PROMPT = """You are reviewing a "procedure" an AI coding agent extracted from its own work session, deciding whether it belongs in the agent's PROCEDURE STORE or is really a standing behavioral rule.

Decide by WHAT THE LESSON IS ABOUT, not by how the principle is phrased ("always/never/before" wording is NOT decisive):

"behavioral_directive" — a lesson about the agent's OWN general working process that applies to ANY task regardless of the technology involved: stating confidence levels, doing due diligence, investigating before planning, verifying assumptions, when to use plan mode / ExitPlanMode / AskUserQuestion, when to ask the user, planning and communication cadence. These describe a universal working HABIT and belong in the agent's standing instructions (CLAUDE.md) — EVEN WHEN they mention meta-tools like ExitPlanMode or AskUserQuestion, because those are incidental to the habit, not its subject.

"task_procedure" — a lesson about operating a SPECIFIC external technical system, tool, library, service, or codebase mechanic: e.g. IMAP inbox consumers, MCP server wiring, Docker layer caching, ripgrep flags, PYTHONPATH, a database, an OAuth flow, a video/transcript API, a specific file or component. Its steps are concrete actions on that system, reusable for that KIND of technical task.

Decisive test: strip away any "always/before/never" framing and ask — is the core lesson about HOW THE AGENT SHOULD WORK IN GENERAL (confidence, diligence, planning, asking) or about HOW A SPECIFIC TECHNICAL SYSTEM WORKS? The former is behavioral_directive; the latter is task_procedure. When genuinely unsure, choose task_procedure (NEVER suppress a real procedure).

Procedure under review:
  task_type: {task_type}
  principle: {principle}
  steps: {steps}

Respond with ONLY this JSON in backticks:
```json
{{"procedure_type": "task_procedure" or "behavioral_directive", "reason": "<one short sentence>"}}
```"""


def _parse_procedure_type(text: str | None) -> str | None:
    """Extract ``procedure_type`` from the classifier response. Returns None on
    any parse failure (caller fails open to keep)."""
    if not text:
        return None
    match = _JSON_BLOCK_RE.search(text)
    raw = match.group(1) if match else text
    ptype: str | None = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            ptype = data.get("procedure_type")
    except (json.JSONDecodeError, TypeError):
        # Lenient fallback: the label may appear in prose.
        low = text.lower()
        if PROCEDURE_TYPE_DIRECTIVE in low and PROCEDURE_TYPE_TASK not in low:
            ptype = PROCEDURE_TYPE_DIRECTIVE
        elif PROCEDURE_TYPE_TASK in low:
            ptype = PROCEDURE_TYPE_TASK
    if ptype not in (PROCEDURE_TYPE_TASK, PROCEDURE_TYPE_DIRECTIVE):
        return None
    return ptype


async def is_behavioral_directive(
    router: Any,
    *,
    task_type: str,
    principle: str,
    steps: list | None,
) -> bool:
    """Return True iff this extracted procedure is really a behavioral directive
    (and should be kept OUT of the procedure store).

    Fails OPEN (returns False = keep) on ANY error, unsuccessful call, or
    unparseable / unknown classification — never suppress a real procedure on a
    flaky classifier. The whole body is wrapped so that no exception can reach
    the caller (where it would be treated as a failed extraction and drop the
    procedure — i.e. fail CLOSED, the opposite of what we want).
    """
    try:
        prompt = _CLASSIFIER_PROMPT.format(
            task_type=task_type,
            principle=(principle or "")[:600],
            steps=json.dumps(steps or [])[:800],
        )
        try:
            result = await router.route_call(
                call_site_id=_CALL_SITE,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            logger.warning(
                "procedure scoping: classifier call failed for %s; keeping",
                task_type, exc_info=True,
            )
            return False

        if not getattr(result, "success", False):
            logger.warning(
                "procedure scoping: classifier unsuccessful for %s (%s); keeping",
                task_type, getattr(result, "error", None),
            )
            return False

        ptype = _parse_procedure_type(getattr(result, "content", None))
        if ptype is None:
            logger.warning(
                "procedure scoping: unparseable classification for %s; keeping",
                task_type,
            )
            return False

        is_directive = ptype == PROCEDURE_TYPE_DIRECTIVE
        if is_directive:
            logger.info(
                "procedure scoping: %s classified as behavioral_directive "
                "(belongs in CLAUDE.md) — not stored",
                task_type,
            )
        return is_directive
    except Exception:
        logger.warning(
            "procedure scoping: unexpected error for %s; keeping", task_type,
            exc_info=True,
        )
        return False
