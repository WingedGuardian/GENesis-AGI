"""Contact tracking — detects and maintains user contacts from extractions.

Identifies person entities in extractions, deduplicates against existing
contacts, and maintains mention counts and context notes.

Called from extraction_job.py as a post-processor, parallel to SVO event
creation and typed link creation.
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from genesis.memory.extraction import Extraction

logger = logging.getLogger(__name__)

# Heuristics for detecting person names in entity lists.
# Person names are typically 2-3 capitalized words without technical jargon.
_NON_PERSON_INDICATORS = frozenset({
    "genesis", "claude", "opus", "sonnet", "haiku",
    "github", "discord", "telegram", "slack", "medium",
    "python", "javascript", "typescript", "rust", "go",
    "aws", "gcp", "azure", "docker", "kubernetes",
    "api", "sdk", "cli", "ui", "ux",
    "pr", "ci", "cd", "db", "sql",
    ".py", ".js", ".ts", ".md", ".yaml", ".json",
    "http", "https", "localhost", "ssh",
})


def _is_likely_person(entity: str) -> bool:
    """Heuristic: is this entity likely a person name?

    Person names are typically 2-3 words, each capitalized, without
    technical indicators.
    """
    if not entity or len(entity) < 3:
        return False

    # Check for non-person indicators
    entity_lower = entity.lower()
    if any(ind in entity_lower for ind in _NON_PERSON_INDICATORS):
        return False

    # Person names are typically 2-3 words
    words = entity.split()
    if len(words) < 2 or len(words) > 4:
        return False

    # Each word should start with uppercase (proper noun pattern)
    if not all(w[0].isupper() for w in words if w):
        return False

    # No digits in person names
    return not any(c.isdigit() for c in entity)


def _find_best_contact_match(
    name: str, contacts: list[dict], threshold: float = 0.85,
) -> dict | None:
    """Find the best matching contact by name similarity.

    Uses SequenceMatcher with a high threshold (0.85) to avoid
    phantom contacts from fuzzy matching.
    """
    name_lower = name.lower()
    best_match = None
    best_score = 0.0

    for contact in contacts:
        contact_name_lower = contact["name"].lower()
        # Exact match (case-insensitive)
        if name_lower == contact_name_lower:
            return contact
        # Fuzzy match
        score = SequenceMatcher(None, name_lower, contact_name_lower).ratio()
        if score > best_score and score >= threshold:
            best_score = score
            best_match = contact

    return best_match


async def process_extraction(
    db: aiosqlite.Connection,
    extraction: Extraction,
    *,
    source_session_id: str | None = None,
) -> int:
    """Process a single extraction for person entities.

    Returns the number of contacts created or updated.
    """
    if not extraction.entities:
        return 0

    # Filter for likely person names
    person_names = [e for e in extraction.entities if _is_likely_person(e)]
    if not person_names:
        return 0

    from genesis.db.crud import user_contacts

    # Load existing contacts for dedup
    existing = await user_contacts.list_all(db, limit=200)
    count = 0

    for name in person_names:
        match = _find_best_contact_match(name, existing)
        if match:
            # Update existing contact
            await user_contacts.record_mention(
                db, match["id"],
                context=extraction.content[:200],
            )
            logger.debug("Contact mention recorded: %s", name)
        else:
            # Create new contact
            contact_id = await user_contacts.create(
                db,
                name=name,
                source=f"extraction:{source_session_id or 'unknown'}",
                relevance=extraction.content[:200],
            )
            # Add to existing list for same-chunk dedup
            existing.append({
                "id": contact_id,
                "name": name,
            })
            logger.info("New contact detected: %s", name)
        count += 1

    return count
