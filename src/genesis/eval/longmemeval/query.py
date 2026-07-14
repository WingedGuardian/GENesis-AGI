"""Convert a LongMemEval question into a Genesis recall query.

Two arms (the harness runs both and reports each, per the design decision):
  * ``RAW`` — the question verbatim (measures recall as any caller uses it).
  * ``KEYWORD`` — question/stop words removed so content terms drive the FTS
    (lexical) arm. The spike showed a raw question returns 0 FTS hits while the
    keyword ``degree`` returns the evidence turn as the top lexical hit.

Keyword extraction is a deterministic stopword strip (no LLM) so the metric is
reproducible; an LLM extractor is a possible future enhancement.
"""

from __future__ import annotations

import re
from enum import StrEnum

_TOKEN = re.compile(r"[a-z0-9']+")

# Function words + WH/aux/pronouns/determiners/common prepositions. Content
# nouns/verbs (name, degree, graduate, restaurant, job, ...) are deliberately
# NOT here.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "it",
        "its",
        "they",
        "them",
        "their",
        "theirs",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "doing",
        "done",
        "have",
        "has",
        "had",
        "having",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "about",
        "as",
        "into",
        "than",
        "then",
        "and",
        "or",
        "but",
        "if",
        "so",
        "not",
        "no",
        "s",
        "t",
        "'s",
    },
)


class QueryArm(StrEnum):
    RAW = "raw"
    KEYWORD = "keyword"


def extract_keywords(question: str) -> str:
    """Strip stop/question words, keeping content tokens in order.

    Falls back to the original question if nothing survives (a degenerate
    all-stopword question must never become an empty recall query).
    """
    kept = [t for t in _TOKEN.findall(question.lower()) if t not in _STOPWORDS]
    return " ".join(kept) if kept else question


def build_query(question: str, arm: QueryArm) -> str:
    """Produce the recall query string for the given arm."""
    if arm is QueryArm.KEYWORD:
        return extract_keywords(question)
    return question
