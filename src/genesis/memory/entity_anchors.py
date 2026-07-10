"""Mechanical code-anchor extraction — regex only, zero LLM, zero ambiguity.

Anchors are exact identifiers whose norm_name IS the identifier: repo
file paths, dotted ``genesis.*`` symbols, PR numbers, commit SHAs. They
feed the entity layer from ``MemoryStore.store()`` (every write path)
and power the anchor-revision belief-updater (E5): anchor's file/symbol
gone from the code index ⇒ candidate for ``invalid_at``.

Deliberately conservative patterns — a missed anchor costs a hop, a
false anchor pollutes the graph. Prompt keywords / activity logs are
NOT sources in v1 (content + provenance beat breadth).
"""

from __future__ import annotations

import re

import aiosqlite

MENTION_CONFIDENCE = 0.9  # mechanical match in the memory's own content

_PATH_RE = re.compile(
    r"\b(?:src|tests|scripts|docs|config)/[A-Za-z0-9_\-./]+\.[A-Za-z]{1,6}\b"
)
_SYMBOL_RE = re.compile(r"\bgenesis(?:\.[a-z_][a-z0-9_]*){1,6}\b")
_PR_RE = re.compile(r"\bPR\s?#(\d{1,6})\b|(?<![\w#])#(\d{2,6})\b")
# Require ≥1 digit so all-letter hex-alphabet words ("deadbee...") skip.
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")

_MAX_ANCHORS_PER_MEMORY = 16


def extract_anchors(text: str) -> list[tuple[str, str]]:
    """``[(name, entity_type)]`` — deduped, ordered, capped."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def _add(name: str, entity_type: str) -> None:
        if name not in seen and len(out) < _MAX_ANCHORS_PER_MEMORY:
            seen.add(name)
            out.append((name, entity_type))

    for m in _PATH_RE.finditer(text):
        _add(m.group(0).rstrip("."), "code_file")
    for m in _SYMBOL_RE.finditer(text):
        # Skip if it's a fragment of a matched path (paths use /, not .)
        _add(m.group(0), "code_symbol")
    for m in _PR_RE.finditer(text):
        number = m.group(1) or m.group(2)
        _add(f"pr#{number}", "pr")
    for m in _SHA_RE.finditer(text):
        _add(m.group(0)[:12], "commit")
    return out


async def record_anchors(
    db: aiosqlite.Connection,
    memory_id: str,
    content: str,
    *,
    source: str = "mechanical",
) -> int:
    """Resolve + mention every anchor in *content*. Returns count.

    Failure-isolated by the caller (the store() seam wraps this in a
    suppress) — this function itself only touches the entity tables.
    """
    from genesis.db.crud import entities as entities_crud
    from genesis.memory.entity_registry import resolve_entity

    anchors = extract_anchors(content)
    for name, entity_type in anchors:
        entity_id, provenance = await resolve_entity(
            db, name=name, entity_type=entity_type, source=source,
            aliases={}, _commit=False,
        )
        await entities_crud.upsert_mention(
            db, memory_id=memory_id, entity_id=entity_id,
            provenance=provenance, confidence=MENTION_CONFIDENCE,
            source=source, _commit=False,
        )
    if anchors:
        await db.commit()
    return len(anchors)
