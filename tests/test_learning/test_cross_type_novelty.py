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
    # The real router always sets BOTH of these (`provider_used=provider_name`,
    # `model_id=provider_cfg.model_id`), and the suppression path reads both, so
    # a stub carrying only one would feed the consumer a shape the producer
    # never emits — the exact way a hand-built stub hides the thing under test.
    # They default to the VALIDATED PAIR because a healthy chain answers from
    # its first rung.
    provider_used: str | None = "openrouter-deepseek-v4"
    model_id: str | None = "deepseek/deepseek-v4-pro"


def _vec(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _embedder_returning(vec: list[float]) -> MagicMock:
    e = MagicMock()
    e.embed = AsyncMock(return_value=list(vec))
    return e


def _router_38a(
    redundant_with,
    provider="openrouter-deepseek-v4",
    model="deepseek/deepseek-v4-pro",
) -> MagicMock:
    r = MagicMock()
    r.route_call = AsyncMock(
        return_value=_Result(
            content=json.dumps({"redundant_with": redundant_with}),
            provider_used=provider,
            model_id=model,
        ),
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
async def test_an_unvalidated_rung_may_answer_but_may_not_suppress(db):
    """The second rung buys AVAILABILITY, and must not buy DELETION with it.

    This call site's declared safety property is precision, and the evidence
    for it — zero false-merges — was measured on deepseek-v4 alone; the second
    rung matches it on TIER, which is not the same claim. A `redundant_with`
    verdict destroys the new procedure silently, so a rung whose false-merge
    rate is unmeasured must not be able to produce one.

    The shipped chain is one rung, so on a stock install nothing else can
    answer. This pins the guard anyway, because a local overlay CAN append a
    rung to a call site's chain — and an unvalidated rung that could suppress
    would be strictly worse than the outage such a rung is usually added to
    survive, trading "duplicates stored" for "real procedures destroyed".
    """
    await _seed(db, "reindex-gitnexus", _vec(0))
    # An overlay-added rung, answering with its own model.
    router = _router_38a(1, provider="glm51", model="zai/glm-5.1")

    is_novel, _max_sim, _vec_out, fell_open = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )

    assert is_novel is True, "an unvalidated rung suppressed a procedure"
    router.route_call.assert_awaited_once(), "the rung must still be ASKED"


@pytest.mark.asyncio
async def test_a_repointed_alias_cannot_inherit_the_measured_model_authority(db):
    """The alias is a MUTABLE handle; the evidence belongs to the model.

    A local overlay is deep-merged into the provider definitions, and the
    overlay sanitizer filters only stale call-site chain entries — it does not
    protect a provider's `model:`. So repointing `openrouter-deepseek-v4` at a
    different model keeps the alias in `provider_used` while `model_id` changes,
    and a check on the alias alone would hand the measured model's authority to
    DELETE procedures to a model nobody measured.

    This is the case that makes the constant a mapping rather than a set.
    """
    await _seed(db, "reindex-gitnexus", _vec(0))
    router = _router_38a(1, provider="openrouter-deepseek-v4", model="some/other-model")

    is_novel, _max_sim, _vec_out, _fo = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )

    assert is_novel is True, (
        "a repointed alias suppressed a procedure — the validated model's "
        "authority transferred to an unmeasured one"
    )


@pytest.mark.asyncio
async def test_the_validated_pair_still_suppresses(db):
    """Control. Without it, every assertion above is satisfied by a guard that
    rejects EVERYTHING, which would disable dedup entirely while looking safe.
    """
    await _seed(db, "reindex-gitnexus", _vec(0))
    router = _router_38a(1)  # defaults ARE the validated pair

    is_novel, _max_sim, _vec_out, _fo = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )

    assert is_novel is False, "the validated pair must still be able to suppress"


@pytest.mark.asyncio
async def test_an_unknown_provider_cannot_suppress_either(db):
    """Fails toward STORING, which is the direction this path already prefers.

    A result carrying no provider at all (an older stub, a future router shape)
    must not inherit the validated provider's authority by default.
    """
    await _seed(db, "reindex-gitnexus", _vec(0))
    router = _router_38a(1, provider=None)

    is_novel, _max_sim, _vec_out, _fo = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )

    assert is_novel is True


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


def _router_38a_all_open() -> MagicMock:
    """Router whose 38a novelty chain has NO available provider (all breakers open)."""
    from types import SimpleNamespace

    r = MagicMock()
    r.route_call = AsyncMock(return_value=_Result(content="{}"))
    r.breakers.chain_has_available = MagicMock(return_value=False)
    r.config = SimpleNamespace(
        call_sites={
            "38a_procedure_novelty_llm": SimpleNamespace(
                chain=["nvidia-nim-deepseek", "openrouter-deepseek-v4"],
            ),
        },
    )
    return r


@pytest.mark.asyncio
async def test_cross_type_fail_fast_when_chain_breaker_open(db):
    """L2c: when the 38a chain has no available provider, skip the doomed
    route_call (no all_exhausted storm) and fail open to 'novel'."""
    await _seed(db, "reindex-gitnexus", _vec(0))
    router = _router_38a_all_open()
    is_novel, _max_sim, _vec_out, _fo = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=router, new_steps=["s"],
    )
    assert is_novel is True  # fail-open to novel — never a false-merge
    router.route_call.assert_not_awaited()  # the doomed 38a call was skipped
    router.breakers.chain_has_available.assert_called_once()


@pytest.mark.asyncio
async def test_cross_type_proceeds_when_call_site_absent(db):
    """L2c guard falls through to the normal route_call when the 38a call site
    is absent from config (unknown/None chain must NOT skip)."""
    from types import SimpleNamespace

    await _seed(db, "reindex-gitnexus", _vec(0))
    r = MagicMock()
    r.route_call = AsyncMock(
        return_value=_Result(content=json.dumps({"redundant_with": None})),
    )
    r.breakers.chain_has_available = MagicMock(return_value=False)  # would skip IF reached
    r.config = SimpleNamespace(call_sites={})  # 38a not present -> _site is None
    is_novel, _m, _v, _fo = await _principle_is_novel(
        db, task_type="reindex-code-intel", new_principle="reindex the code graph",
        embedder=_embedder_returning(_vec(0)), router=r, new_steps=["s"],
    )
    assert is_novel is True
    r.route_call.assert_awaited_once()  # guard skipped -> normal call fired
    r.breakers.chain_has_available.assert_not_called()  # short-circuit on _chain None
