"""The duplicate check must see the same text the store will persist.

``MemoryStore.store()`` normalizes surface forms (alias expansion, e.g.
``"CC" -> "Claude Code"``) and separately short-circuits when
``find_exact_duplicate`` matches content already stored. That lookup matches
``memory_fts`` content EXACTLY, and what lands in ``memory_fts`` is the
NORMALIZED text — so the order of those two steps decides whether a store can
find its own previous row.

With normalization running AFTER the lookup: a store of ``"CC owns the gate"``
persisted ``"Claude Code owns the gate"``, and the next store of the same raw
text queried for ``"CC owns the gate"``, missed the row it had just written, and
wrote a second copy whose stored content was byte-identical to the first.
Aliases are seeded by default (``entity_resolution._SEED_ALIASES``), so this
needed no configuration to happen.

Asserted at the seam — the value handed to the lookup — rather than through the
whole pipeline ON PURPOSE: the lookup is handed a value, and that value IS the
defect. Driving it through the full ``store()`` write path instead makes the
mutation fail on unrelated fixture-schema errors, which proves nothing about
the ordering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore

# Deterministic aliases, so the test does not depend on whatever alias file
# happens to exist on the machine running it. The seed dictionary ships with
# exactly this mapping.
ALIASES = {"CC": "Claude Code"}
RAW = "CC owns the review gate"
CANONICAL = "Claude Code owns the review gate"


@pytest.fixture()
def store():
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    return MemoryStore(
        embedding_provider=ep,
        qdrant_client=MagicMock(),
        db=AsyncMock(),
        linker=MagicMock(),
    )


@pytest.mark.asyncio()
async def test_dedup_lookup_sees_the_normalized_text(store):
    """The lookup is handed the canonical form, which is what gets persisted."""
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    seen: dict[str, str] = {}

    async def spy(_db, *, content):
        seen["content"] = content
        # ALWAYS short-circuit, so the full write pipeline never runs and the
        # only thing that can fail is the assertion below.
        return "pre-existing-id"

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", spy),
    ):
        returned = await store.store(RAW, "conversation")

    assert returned == "pre-existing-id"
    assert seen["content"] == CANONICAL, (
        "the dedup lookup was handed the RAW text while the store persists the "
        f"normalized form ({seen['content']!r} != {CANONICAL!r}) — a second "
        "store of the same raw text can never match the row the first one wrote"
    )


@pytest.mark.asyncio()
async def test_normalization_failure_still_lets_the_store_proceed(store):
    """Normalization stays best-effort: it must never block a store.

    Locks the `except Exception: pass` that moved with the block. Without it,
    a broken alias file would turn every store into a raised error.
    """
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    seen: dict[str, str] = {}

    def boom():
        raise RuntimeError("alias file unreadable")

    async def spy(_db, *, content):
        seen["content"] = content
        return "pre-existing-id"

    with (
        patch.object(er, "load_aliases", boom),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", spy),
    ):
        returned = await store.store(RAW, "conversation")

    assert returned == "pre-existing-id"
    assert seen["content"] == RAW, "un-normalized is the correct fallback"
