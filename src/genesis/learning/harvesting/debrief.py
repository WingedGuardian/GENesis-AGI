"""Parse learnings from CC session output."""

from __future__ import annotations

import json
import re


def parse_debrief(text: str) -> list[str]:
    """Extract learnings from CC session output.

    Supports two formats:
    1. JSON: {"learnings": ["lesson1", "lesson2"]}
    2. Markdown: ## Learnings\\n- lesson1\\n- lesson2

    Returns empty list if no learnings section found.
    """
    if not text:
        return []

    # Try JSON first
    match = re.search(r"\{[^{}]*\"learnings\"[^{}]*\}", text)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data.get("learnings"), list):
                return data["learnings"]
        except (json.JSONDecodeError, KeyError):
            pass

    # Try markdown
    md_match = re.search(
        r"##\s+Learnings\s*\n((?:\s*[-*]\s+.+\n?)+)", text, re.IGNORECASE
    )
    if md_match:
        lines = md_match.group(1).strip().splitlines()
        return [re.sub(r"^\s*[-*]\s+", "", line).strip() for line in lines if line.strip()]

    return []
