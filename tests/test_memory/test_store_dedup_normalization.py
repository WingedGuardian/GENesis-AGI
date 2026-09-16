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


# ── A duplicate does not discharge the supersession ──────────────────────────
#
# `store(content=<dup>, supersedes=X)` asks for TWO things. Returning early on
# the dedup hit satisfied the first and dropped the second in silence, while
# the API reported success and X stayed live. These pin that the second one
# still happens, and — the control — that it happens only when it was asked for.


@pytest.mark.asyncio()
async def test_a_duplicate_still_performs_the_requested_supersession(store):
    """The stale memory must be deprecated onto the row that already has it."""
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    marked: dict[str, str] = {}

    async def hit(_db, *, content):
        return "pre-existing-id"

    async def resolve(handle):
        return f"resolved::{handle}"

    async def mark(old_id, new_id, _stamp, **_kw):
        marked["old"], marked["new"] = old_id, new_id

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", hit),
        patch.object(store, "_resolve_supersede_target", resolve),
        patch.object(store, "_mark_superseded", mark),
    ):
        returned = await store.store(RAW, "conversation", supersedes="stale-handle")

    assert returned == "pre-existing-id"
    assert marked == {"old": "resolved::stale-handle", "new": "pre-existing-id"}, (
        "the dedup short-circuit returned success without performing the "
        "supersession the caller asked for — the stale memory stays live and "
        "nothing reports it"
    )


@pytest.mark.asyncio()
async def test_a_duplicate_without_supersedes_marks_nothing(store):
    """Control for the case above.

    Without this, "a duplicate supersedes" is equally satisfied by a version
    that supersedes unconditionally — which would deprecate a row the caller
    never named.
    """
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    calls: list[object] = []

    async def hit(_db, *, content):
        return "pre-existing-id"

    async def mark(*a, **kw):
        calls.append((a, kw))

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", hit),
        patch.object(store, "_mark_superseded", mark),
    ):
        returned = await store.store(RAW, "conversation")

    assert returned == "pre-existing-id"
    assert calls == [], "nothing was superseded, so nothing may be marked"


@pytest.mark.asyncio()
async def test_an_unresolvable_pair_raises_without_writing_a_duplicate(store):
    """The pair's error must reach the caller INSTEAD of a write, not after one.

    The supersede work sits inside the dedup block's `try`, whose
    `except Exception` is deliberately best-effort, so without the narrower
    `except SupersedeUnresolved: raise` ahead of it the failure is swallowed
    and execution falls through to the ordinary store path.

    The discriminating case is a failure in `_mark_superseded`, not in
    `_resolve_supersede_target`. A bad HANDLE fails identically either way —
    the ordinary path re-resolves it and raises the same error before writing
    anything — which is exactly what an earlier version of this test asserted,
    and why a mutation deleting the re-raise SURVIVED it. A bad PAIR is
    different: the handle resolves fine, so falling through writes a duplicate
    memory and then supersedes onto the row it just created, while the defect
    in the pair is never reported.
    """
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod
    from genesis.memory.store import SupersedeUnresolved

    async def hit(_db, *, content):
        return "pre-existing-id"

    async def resolve(handle):
        return f"resolved::{handle}"

    async def mark(old_id, new_id, _stamp, **_kw):
        # e.g. the duplicate row is itself deprecated, or old == new.
        raise SupersedeUnresolved(old_id, "successor_deprecated", successor_id=new_id)

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", hit),
        patch.object(store, "_resolve_supersede_target", resolve),
        patch.object(store, "_mark_superseded", mark),
        pytest.raises(SupersedeUnresolved),
    ):
        await store.store(RAW, "conversation", supersedes="live-handle")

    store.embedding_provider.embed.assert_not_awaited()


# ── Both surface forms are checked ───────────────────────────────────────────
#
# `load_aliases()` is mtime-driven and best-effort, so an alias added AFTER a
# row was written — or a normalization that failed once and later recovered —
# leaves the RAW text in memory_fts. Querying only the normalized form misses
# that row and mints the duplicate normalizing-first exists to prevent.


@pytest.mark.asyncio()
async def test_the_raw_form_is_checked_when_the_normalized_form_misses(store):
    """A row written before the alias existed is still its own duplicate."""
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    queried: list[str] = []

    async def by_form(_db, *, content):
        queried.append(content)
        # Only the RAW text is in the index — the row predates the alias.
        return "legacy-raw-id" if content == RAW else None

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", by_form),
    ):
        returned = await store.store(RAW, "conversation")

    assert queried == [CANONICAL, RAW], (
        "the raw surface form was never queried, so a row written before the "
        f"alias existed is invisible to dedup (queried {queried!r})"
    )
    assert returned == "legacy-raw-id"


@pytest.mark.asyncio()
async def test_a_normalized_hit_does_not_also_query_the_raw_form(store):
    """Control for the case above: the second lookup is a FALLBACK, not a pair.

    Without this, "both forms are checked" is satisfied by querying twice
    every time, which doubles the lookup cost of the common path.
    """
    import genesis.memory.entity_resolution as er
    import genesis.memory.store as store_mod

    queried: list[str] = []

    async def by_form(_db, *, content):
        queried.append(content)
        return "canonical-id"

    with (
        patch.object(er, "load_aliases", lambda: ALIASES),
        patch.object(store_mod.memory_crud, "find_exact_duplicate", by_form),
    ):
        returned = await store.store(RAW, "conversation")

    assert queried == [CANONICAL], "the normalized hit settles it"
    assert returned == "canonical-id"
