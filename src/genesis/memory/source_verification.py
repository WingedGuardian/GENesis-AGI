"""Structural source-overlap verification for memory extractions.

Checks that extracted claims actually appear in the source transcript.
No LLM calls — pure lexical analysis. Inspired by anneal-memory's
citation-validated graduation pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Common English stopwords — kept minimal and inline to avoid dependencies
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about", "up",
    "that", "this", "these", "those", "it", "its", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "they", "them",
    "their", "what", "which", "who", "whom",
})

_WORD_RE = re.compile(r"[a-z0-9_]+(?:[-'][a-z0-9]+)*", re.IGNORECASE)


@dataclass(frozen=True)
class OverlapResult:
    """Result of a source-overlap verification check."""
    verified: bool
    overlap: float  # 0.0-1.0 overlap score
    extraction_terms: int  # meaningful terms in extraction
    matched_terms: int  # terms found in source


@dataclass(frozen=True)
class DedupResult:
    """Result of a cross-session claim dedup check."""
    is_duplicate: bool
    jaccard: float
    matched_memory_id: str | None = None


def _extract_terms(text: str) -> set[str]:
    """Extract meaningful terms from text, lowercased, stopwords removed."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def verify_source_overlap(
    extraction_content: str,
    source_text: str,
    *,
    threshold: float = 0.4,
) -> OverlapResult:
    """Check that extraction content overlaps with the source transcript.

    Args:
        extraction_content: The extracted claim/fact.
        source_text: The source transcript chunk it was extracted from.
        threshold: Minimum fraction of extraction terms that must appear
            in the source. Default 0.4 (40%).

    Returns:
        OverlapResult with verified=True if overlap >= threshold.
    """
    if not extraction_content or not source_text:
        return OverlapResult(verified=False, overlap=0.0,
                             extraction_terms=0, matched_terms=0)

    ext_terms = _extract_terms(extraction_content)
    src_terms = _extract_terms(source_text)

    if not ext_terms:
        return OverlapResult(verified=False, overlap=0.0,
                             extraction_terms=0, matched_terms=0)

    matched = ext_terms & src_terms
    overlap = len(matched) / len(ext_terms)

    return OverlapResult(
        verified=overlap >= threshold,
        overlap=round(overlap, 3),
        extraction_terms=len(ext_terms),
        matched_terms=len(matched),
    )


def compute_jaccard(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts (stopword-filtered)."""
    terms_a = _extract_terms(text_a)
    terms_b = _extract_terms(text_b)
    if not terms_a or not terms_b:
        return 0.0
    intersection = terms_a & terms_b
    union = terms_a | terms_b
    return len(intersection) / len(union)
