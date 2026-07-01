"""Stream 2: Procedure candidate classifier (flag-only).

Runs over each chunk's extraction output (alongside reference_extraction.py).
Classifies ``procedure_candidate`` extractions into lightweight candidate dicts.

C2b: this path no longer calls the Judge per candidate. The classified candidates
are returned as a SESSION-LEVEL SIGNAL — their presence tells the whole-session
struggle builder (``learning/procedural/judge.judge_multi_procedure``) to run even
when the struggle score is below threshold. Procedure CONTENT is reconstructed by
that builder from the action spine, not stored here. Pure, zero-LLM classification.
"""

from __future__ import annotations

from genesis.memory.extraction import Extraction


def classify_as_procedure(extraction: Extraction) -> dict | None:
    """Classify an extraction as a procedure candidate.

    Returns a dict with scenario/principle/tools or None if not a candidate.
    Pure classifier — no LLM calls. The heavy lifting is done by the builder.
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


def extract_procedures_from_chunk(extractions: list[Extraction]) -> list[dict]:
    """Classify chunk extractions into procedure candidates (flag-only signal).

    Returns the list of candidate dicts (possibly empty). Stores nothing and
    makes no LLM calls — the caller accumulates candidates across chunks and uses
    their existence to trigger the whole-session builder.
    """
    candidates: list[dict] = []
    for ext in extractions:
        candidate = classify_as_procedure(ext)
        if candidate is not None:
            candidates.append(candidate)
    return candidates
