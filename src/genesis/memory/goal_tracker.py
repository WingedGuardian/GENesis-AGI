"""Goal signal tracking — detects and maintains user goals from extractions.

Consumes extractions that mention goals, aspirations, or direction changes.
Writes to the user_goals table via CRUD operations. Deduplicates against
existing goals using title similarity.

Called from extraction_job.py as a post-processor, parallel to SVO event
creation and typed link creation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from genesis.memory.extraction import Extraction

logger = logging.getLogger(__name__)

# Keywords that indicate a goal or aspiration in extraction content.
# These are checked against the extraction content, not the type.
_GOAL_KEYWORDS = frozenset({
    "goal", "aspiration", "objective", "target", "aim",
    "want to", "plan to", "trying to", "working toward",
    "career", "job search", "freelance", "employment",
    "thought leadership", "networking", "outreach",
    "build", "launch", "publish", "ship",
})

# Map extraction content keywords to goal categories.
_CATEGORY_SIGNALS = {
    "career": {"career", "job", "freelance", "employment", "hire", "role", "salary", "resume"},
    "project": {"build", "ship", "launch", "implement", "deploy", "release", "feature"},
    "learning": {"learn", "study", "research", "understand", "explore", "course"},
    "relationship": {"network", "outreach", "connect", "meet", "introduce", "mentor"},
    "financial": {"budget", "revenue", "cost", "income", "savings", "investment"},
}


def _detect_goal_signal(extraction: Extraction) -> dict | None:
    """Check if an extraction contains a goal signal.

    Returns a dict with goal metadata if detected, None otherwise.
    Only fires on high-confidence extractions with goal-related content.
    """
    if extraction.confidence < 0.7:
        return None

    content_lower = extraction.content.lower()

    # Check if any goal keyword appears in the content
    has_goal_keyword = any(kw in content_lower for kw in _GOAL_KEYWORDS)
    if not has_goal_keyword:
        return None

    # Determine category from content
    category = "other"
    best_count = 0
    for cat, signals in _CATEGORY_SIGNALS.items():
        count = sum(1 for s in signals if s in content_lower)
        if count > best_count:
            best_count = count
            category = cat

    return {
        "title": extraction.content[:200],
        "category": category,
        "confidence": extraction.confidence,
        "evidence": extraction.content,
    }


async def process_extraction(
    db: aiosqlite.Connection,
    extraction: Extraction,
    *,
    source_session_id: str | None = None,
) -> bool:
    """Process a single extraction for goal signals.

    Returns True if a goal was created or updated.
    """
    signal = _detect_goal_signal(extraction)
    if not signal:
        return False

    from genesis.db.crud import user_goals

    # Check for existing similar goal
    existing = await user_goals.find_similar(db, signal["title"])
    if existing:
        # Update confidence and add progress note
        new_conf = max(existing["confidence"], signal["confidence"])
        await user_goals.update(db, existing["id"], confidence=new_conf)
        await user_goals.add_progress_note(
            db, existing["id"],
            f"Signal reinforced: {signal['evidence'][:100]}",
        )
        logger.debug(
            "Goal signal reinforced existing goal %s: %s",
            existing["id"][:8], existing["title"][:60],
        )
        return True

    # Create new goal
    await user_goals.create(
        db,
        title=signal["title"],
        category=signal["category"],
        confidence=signal["confidence"],
        evidence_source=f"extraction:{source_session_id or 'unknown'}",
    )
    logger.info(
        "New goal detected from extraction: %s (%s, conf=%.2f)",
        signal["title"][:60], signal["category"], signal["confidence"],
    )
    return True
