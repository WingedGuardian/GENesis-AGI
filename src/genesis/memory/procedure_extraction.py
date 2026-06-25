"""Stream 2: Procedure candidate post-processor.

Runs over each chunk's extraction output (alongside reference_extraction.py).
Classifies procedure_candidate extractions and routes them to the Judge LLM
for validation and storage.

Pattern: mirrors reference_extraction.py — zero extra LLM calls for
classification, paid LLM call only when the Judge is invoked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from genesis.memory.extraction import Extraction

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Timeout for Judge LLM calls. Must match judge.JUDGE_TIMEOUT_SECS.
# Kept as local constant to avoid top-level memory → learning import.
_JUDGE_TIMEOUT_SECS = 60.0


def classify_as_procedure(extraction: Extraction) -> dict | None:
    """Classify an extraction as a procedure candidate.

    Returns a dict with scenario/principle/tools or None if not a candidate.
    Pure classifier — no LLM calls. The heavy lifting is done by the Judge.
    """
    if extraction.extraction_type != "procedure_candidate":
        return None
    if not extraction.scenario:
        return None
    if len(extraction.content) < 50:
        return None  # Too short to be actionable
    return {
        "scenario": extraction.scenario,
        "principle": extraction.content,
        "tools_used": extraction.entities,
        "context_tags": extraction.entities,
    }


async def extract_procedures_from_chunk(
    extractions: list[Extraction],
    *,
    db: aiosqlite.Connection,
    router,
    source_session_id: str | None = None,
    chunk_context: str = "",
    max_new: int | None = None,
) -> int:
    """Run classifier over chunk extractions, route candidates to Judge.

    Returns count of procedures stored. Each Judge call is individually
    timeout-guarded to prevent a single hung LLM from blocking the
    entire extraction loop. ``max_new`` caps how many procedures this call may
    store (the caller's remaining per-session budget); once reached, no further
    candidates are judged.
    """
    # Deferred import — memory → learning direction
    from genesis.learning.procedural.judge import judge_extraction_candidate

    count = 0
    for ext in extractions:
        if max_new is not None and count >= max_new:
            break
        candidate = classify_as_procedure(ext)
        if candidate is None:
            continue

        try:
            result = await asyncio.wait_for(
                judge_extraction_candidate(
                    db, candidate, chunk_context, router,
                    source_session_id=source_session_id,
                ),
                timeout=_JUDGE_TIMEOUT_SECS,
            )
            if result is not None:
                count += 1
        except TimeoutError:
            logger.warning(
                "Judge timed out on procedure candidate (session %s)",
                source_session_id,
            )
        except Exception:
            logger.warning(
                "Judge failed on procedure candidate", exc_info=True,
            )

    return count
