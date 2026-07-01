"""Tests for the C2b cross-task_type novelty gate (extractor._principle_is_novel).

Covers: a cross-type paraphrase is caught via the LLM dedup; a distinct one is
kept; the same-type compare reads STORED embedding BLOBs (no re-embed); and
deprecated rows never suppress a new procedure (list_by_task_type / list_active
both exclude them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural import embedding as embedding_mod
from genesis.learning.procedural.embedding import EMBEDDING_DIM, pack_embedding
from genesis.learning.procedural.extractor import _principle_is_novel
from genesis.learning.procedural.operations import store_procedure


@dataclass
class _Result:
    success: bool = True
    content: str = ""
    error: str | None = None


def _vec(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _embedder_returning(vec: list[float]) -> MagicMock:
    e = MagicMock()
    e.embed = AsyncMock(return_value=list(vec))
    return e


def _router_38a(redundant_with) -> MagicMock:
    r = MagicMock()
    r.route_call = AsyncMock(
        return_value=_Result(content=json.dumps({"redundant_with": redundant_with})),
    )
    return r


@pytest.fixture(autouse=True)
def _reset():
    embedding_mod._EMBEDDING_PROVIDER = None
    embedding_mod._fail_open_timestamps.clear()
    yield
    embedding_mod._fail_open_timestamps.clear()


async def _seed(db, task_type, vec, *, deprecated=0):
    pid = await store_procedure(
        db, task_type=task_type, principle=f"{task_type} principle",
        steps=["s"], tools_used=["Bash"], context_tags=["c"],
        principle_embedding=pack_embedding(vec),
    )
    if deprecated:
        await procedural.update(db, pid, deprecated=1)
    return pid


@pytest.mark.asyncio
async def test_cross_type_duplicate_caught(db):
    await _seed(db, "reindex-gitnexus", _vec(0))
    # New procedure under a DIFFERENT slug, near-identical embedding.
    router = _router_38a(1)  # LLM: redundant with candidate #1
    is_novel, _max_sim, _vec_out, fell_open = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )
    assert is_novel is False
    assert fell_open is False
    router.route_call.assert_awaited_once()  # the 38a dedup call fired


@pytest.mark.asyncio
async def test_cross_type_distinct_kept(db):
    await _seed(db, "reindex-gitnexus", _vec(0))
    router = _router_38a(None)  # LLM: not redundant
    is_novel, _max_sim, _vec_out, _fo = await _principle_is_novel(
        db, task_type="restart-server", new_principle="restart the server cleanly",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )
    assert is_novel is True


@pytest.mark.asyncio
async def test_same_type_uses_stored_blob_no_reembed(db):
    """Existing same-type rows are compared via their stored BLOB — the embedder
    is called only once (for the NEW principle), not per existing row."""
    await _seed(db, "task-a", _vec(0))
    embedder = _embedder_returning(_vec(5))  # orthogonal → passes same-type gate
    is_novel, _max_sim, _v, _fo = await _principle_is_novel(
        db, task_type="task-a", new_principle="a different a-principle",
        embedder=embedder, router=None, new_steps=["s"],
    )
    assert is_novel is True
    embedder.embed.assert_awaited_once()  # only the new principle was embedded


@pytest.mark.asyncio
async def test_deprecated_row_does_not_suppress(db):
    """A deprecated near-duplicate (same task_type, identical embedding) must NOT
    block a new procedure — list_by_task_type/list_active exclude deprecated."""
    await _seed(db, "task-a", _vec(0), deprecated=1)
    router = _router_38a(None)
    is_novel, _max_sim, _v, _fo = await _principle_is_novel(
        db, task_type="task-a", new_principle="same idea, fresh row",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )
    assert is_novel is True  # deprecated dup excluded → stored
