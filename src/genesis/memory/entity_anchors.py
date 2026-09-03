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

MENTION_CONFIDENCE = 0.9  # mechanical match in the memory's own content

_PATH_RE = re.compile(
    r"\b(?:src|tests|scripts|docs|config)/[A-Za-z0-9_\-./]+\.[A-Za-z]{1,6}\b"
)
_SYMBOL_RE = re.compile(r"\bgenesis(?:\.[a-z_][a-z0-9_]*){1,6}\b")
_PR_RE = re.compile(r"\bPR\s?#(\d{1,6})\b|(?<![\w#])#(\d{2,6})\b")
# Require ≥1 digit AND ≥1 hex letter: all-letter words ("deadbee...")
# and plain numeric IDs ("1234567890" — tickets, builds, timestamps)
# both skip. Cost: the ~4% of real 7-char SHA prefixes that are
# all-digit are missed — conservative by design (see module docstring).
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")

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
    memory_id: str,
    content: str,
    *,
    source: str = "mechanical",
) -> int:
    """Resolve + mention every anchor in *content*. Returns count.

    Writes run on a DEDICATED ``get_raw_db()`` connection under a single
    ``BEGIN IMMEDIATE`` … ``COMMIT`` envelope — NOT the caller's shared
    ``SerializedConnection``. On the shared connection the lock releases between
    ops (``db/connection.py:106``), so a CONCURRENT tool call's ``commit()`` /
    ``rollback()`` could durably commit or discard this batch's partial writes
    mid-run; an owned connection gives real single-writer isolation and never
    touches another coroutine's transaction.

    Best-effort + failure-isolated by the caller (``store()`` wraps this in a
    suppress); it only touches the entity tables. Every anchor is a MECHANICAL
    type (``entity_registry.MECHANICAL_TYPES``), so ``resolve_entity``
    short-circuits to a single fast ``create_entity`` — no difflib fuzzy match
    runs inside the write lock, so the ``BEGIN IMMEDIATE`` hold stays short. No
    app-level lock retry: an owned ``get_raw_db`` already waits out the standard
    ``busy_timeout``, and a rare BUSY beyond it just drops these anchors (the
    caller's suppress), which is acceptable for a best-effort enrichment.
    """
    import contextlib

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import entities as entities_crud
    from genesis.env import genesis_db_path
    from genesis.memory.entity_registry import resolve_entity

    anchors = extract_anchors(content)
    if not anchors:
        return 0

    # Function-scope genesis_db_path() (NOT a module-frozen DEFAULT_DB_PATH) so the
    # test conftest redirect applies and a worktree never opens a stray DB.
    async with get_raw_db(genesis_db_path()) as own:
        try:
            await own.execute("BEGIN IMMEDIATE")
            for name, entity_type in anchors:
                entity_id, provenance = await resolve_entity(
                    own, name=name, entity_type=entity_type, source=source,
                    aliases={}, _commit=False,
                )
                await entities_crud.upsert_mention(
                    own, memory_id=memory_id, entity_id=entity_id,
                    provenance=provenance, confidence=MENTION_CONFIDENCE,
                    source=source, _commit=False,
                )
            await own.commit()
        except BaseException:
            # Roll the OWNED txn back on ANY exit (CancelledError included — a
            # BaseException that bypasses `except Exception`); this can never
            # discard another coroutine's uncommitted writes.
            with contextlib.suppress(Exception):
                await own.rollback()
            raise
    return len(anchors)
