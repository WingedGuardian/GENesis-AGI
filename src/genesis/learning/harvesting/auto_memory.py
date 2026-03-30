"""Harvest CC auto-memory directory for Genesis-relevant items."""

from __future__ import annotations

import re
from pathlib import Path

_CC_INTERNAL_PATTERNS = [
    re.compile(r"^#.*Claude Code", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^#.*Session", re.IGNORECASE | re.MULTILINE),
    re.compile(r"context window|token limit|compact", re.IGNORECASE),
]


def harvest_auto_memory(
    memory_dir: Path,
    *,
    exclude_patterns: list[re.Pattern] | None = None,  # type: ignore[type-arg]
) -> list[dict]:
    """Read CC session memory files, filter CC internals, return Genesis-relevant items.

    Returns list of {"file": str, "content": str} for relevant items.
    Skips files matching CC internal patterns.
    """
    patterns = exclude_patterns or _CC_INTERNAL_PATTERNS
    items: list[dict] = []
    if not memory_dir.exists():
        return items
    for f in memory_dir.glob("*.md"):
        content = f.read_text(encoding="utf-8")
        if any(p.search(content) for p in patterns):
            continue
        items.append({
            "file": f.name,
            "content": content,
            "mtime_ns": f.stat().st_mtime_ns,
        })
    return items
