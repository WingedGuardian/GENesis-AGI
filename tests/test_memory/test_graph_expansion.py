"""Recall-time 1-hop graph expansion (``genesis.memory.graph_expansion``).

Covers the three layers of the module:

- config: DEFAULTS ← ``config/memory_recall.yaml`` ← ``.local.yaml`` overlay,
  re-read on EVERY call (no cache — a ``settings_update`` takes effect on the
  next recall in the same process), master ``enabled: false`` short-circuit,
  invalid mode degrading to ``shadow`` (observable, never behavior-changing —
  the ws3_immunity posture).
- ``expand_neighbors``: the mode-independent primitive (also reused by the
  LongMemEval graph arm) — stored-first provenance, deterministic ordering,
  dangling-link skips, seed/exclude discipline, link-type exclusion.
- ``maybe_expand``: the MCP-surface wrapper — off passthrough, shadow
  emit-but-don't-touch, live append, per-surface caps, and the
  metrics-never-raise guarantee.

Config paths are redirected into tmp dirs exactly like
``tests/test_security/test_immunity.py`` (base via ``repo_root``, overlay via
``_user_config_dir`` in both namespaces).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.memory import graph_expansion
from genesis.memory.types import RetrievalResult
from tests.vector_store_fake import FakeVectorStore, point_id


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(graph_expansion, "repo_root", lambda: repo_dir)
    monkeypatch.setattr(
        "genesis._config_overlay._user_config_dir",
        lambda: user_dir,
    )
    # Reset the module-level mtime cache so it can't leak across tests.
    monkeypatch.setattr(graph_expansion, "_config_cache", None, raising=False)

    return (
        repo_dir / "config" / "memory_recall.yaml",
        user_dir / "memory_recall.local.yaml",
    )


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def qdrant() -> FakeVectorStore:
    return FakeVectorStore()


async def _seed_memory(
    db,
    qdrant,
    memory_id: str,
    *,
    content: str | None = None,
    collection: str = "episodic_memory",
    origin_class: str | None = "owner",
) -> None:
    """A stored memory as ``MemoryStore.store()`` leaves it: FTS row, vector
    point, metadata row."""
    text = content if content is not None else f"content of {memory_id}"
    await memory_crud.create(
        db,
        memory_id=memory_id,
        content=text,
        source_type="memory",
        collection=collection,
    )
    qdrant.put(collection, memory_id, content=text, source_type="memory", tags=[])
    await memory_crud.create_metadata(
        db,
        memory_id=memory_id,
        created_at="2026-07-01T00:00:00Z",
        collection=collection,
        origin_class=origin_class,
    )


async def _link(
    db, source: str, target: str, *, strength: float = 0.8, link_type: str = "supports"
) -> None:
    await memory_links_crud.create(
        db,
        source_id=source,
        target_id=target,
        link_type=link_type,
        strength=strength,
        created_at="2026-07-01T00:00:00Z",
    )


def _result(memory_id: str = point_id("seed-1")) -> RetrievalResult:
    return RetrievalResult(
        memory_id=memory_id,
        content=f"content of {memory_id}",
        source="memory",
        memory_type="episodic_memory",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.0,
        payload={},
    )


# ─── config: defaults / layering / live re-read ──────────────────────────────


def test_defaults_with_no_files_at_all(config_dirs):
    cfg = graph_expansion.load_recall_config()
    assert cfg["enabled"] is True
    assert cfg["graph_expansion"]["mode"] == "shadow"
    assert cfg["graph_expansion"]["max_neighbors"] == 10
    assert cfg["graph_expansion"]["proactive_max_neighbors"] == 2
    assert cfg["graph_expansion"]["exclude_link_types"] == ["contradicts"]
    assert cfg["entity_lane"]["mode"] == "off"
    assert graph_expansion.expansion_mode() == "shadow"


def test_base_yaml_overrides_defaults(config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live", "max_neighbors": 5}})
    cfg = graph_expansion.load_recall_config()
    assert cfg["graph_expansion"]["mode"] == "live"
    assert cfg["graph_expansion"]["max_neighbors"] == 5
    # untouched keys keep defaults
    assert cfg["graph_expansion"]["proactive_max_neighbors"] == 2
    assert graph_expansion.expansion_mode() == "live"


def test_local_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    _write(overlay, {"graph_expansion": {"mode": "off"}})
    assert graph_expansion.expansion_mode() == "off"


def test_master_enabled_false_short_circuits_to_off(config_dirs):
    base, _ = config_dirs
    _write(base, {"enabled": False, "graph_expansion": {"mode": "live"}})
    assert graph_expansion.expansion_mode() == "off"


def test_invalid_mode_degrades_to_shadow(config_dirs, caplog):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "banana"}})
    assert graph_expansion.expansion_mode() == "shadow"


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text(":: not yaml ::[")
    assert graph_expansion.expansion_mode() == "shadow"


def test_live_reread_on_file_change(config_dirs):
    # A settings_update / hand-edit changes the file's mtime+size, so the
    # mtime-keyed cache re-reads it on the next recall (the "takes effect on
    # next recall" contract is preserved).
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "off"}})
    assert graph_expansion.expansion_mode() == "off"
    _write(base, {"graph_expansion": {"mode": "live"}})
    assert graph_expansion.expansion_mode() == "live"


def test_config_cached_until_file_changes(config_dirs, monkeypatch):
    # Unchanged files must not be re-parsed on every recall (hot path). Count
    # base-yaml parses: a second load with no file change is served from cache.
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "shadow"}})
    calls = {"n": 0}
    real = graph_expansion.yaml.safe_load

    def _counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(graph_expansion.yaml, "safe_load", _counting)
    graph_expansion.load_recall_config()
    graph_expansion.load_recall_config()
    assert calls["n"] == 1  # second load served from cache
    _write(base, {"graph_expansion": {"mode": "live"}})
    graph_expansion.load_recall_config()
    assert calls["n"] == 2  # file changed → cache invalidated, re-parsed


# ─── expand_neighbors: the mode-independent primitive ────────────────────────


async def test_expand_returns_stored_provenance_and_marker(db, qdrant):
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(
        db,
        qdrant,
        point_id("nbr-1"),
        collection="knowledge_base",
        origin_class="external_untrusted",
    )
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.9)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=10)

    assert len(out) == 1
    r = out[0]
    assert isinstance(r, RetrievalResult)
    assert r.memory_id == point_id("nbr-1")
    assert r.content == f"content of {point_id('nbr-1')}"
    # stored-first provenance: never synthetic values
    assert r.collection == "knowledge_base"
    assert r.origin_class == "external_untrusted"
    assert r.source_pipeline is None  # no SQLite column; never fabricated
    # sorts after any organic result
    assert r.score == pytest.approx(0.01 * 0.9)
    marker = r.payload["graph_expansion"]
    assert marker["linked_from"] == [point_id("seed-1")]
    assert marker["strength"] == pytest.approx(0.9)


async def test_expand_skips_dangling_neighbors(db, qdrant):
    await _seed_memory(db, qdrant, point_id("seed-1"))
    # link rows exist but the neighbor memory does not resolve
    await _link(db, point_id("seed-1"), point_id("ghost-1"), strength=0.9)
    await _seed_memory(db, qdrant, point_id("nbr-1"))
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.5)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=10)

    assert [r.memory_id for r in out] == [point_id("nbr-1")]


async def test_expand_never_returns_a_deleted_memorys_leftover_point(db, qdrant):
    """A deleted memory can leave its vector point behind, payload text and all,
    and auto_link can link to it. Recall drops such a ghost, and so does
    expansion: no metadata row, no neighbour."""
    await _seed_memory(db, qdrant, point_id("seed-1"))
    qdrant.put("episodic_memory", point_id("deleted-1"), content="deleted text", tags=[])
    await _link(db, point_id("seed-1"), point_id("deleted-1"), strength=0.9)
    await _seed_memory(db, qdrant, point_id("nbr-1"))
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.5)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=10)

    assert [r.memory_id for r in out] == [point_id("nbr-1")]


async def test_expand_cap_and_strength_ordering(db, qdrant):
    await _seed_memory(db, qdrant, point_id("seed-1"))
    for i, s in enumerate((0.3, 0.9, 0.6)):
        await _seed_memory(db, qdrant, point_id(f"nbr-{i}"))
        await _link(db, point_id("seed-1"), point_id(f"nbr-{i}"), strength=s)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=2)

    assert [r.memory_id for r in out] == [point_id("nbr-1"), point_id("nbr-2")]  # 0.9, 0.6


async def test_expand_never_returns_seeds_or_excluded(db, qdrant):
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("seed-2"))
    await _seed_memory(db, qdrant, point_id("nbr-1"))
    await _link(db, point_id("seed-1"), point_id("seed-2"), strength=0.9)  # seed↔seed link
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.8)
    await _seed_memory(db, qdrant, point_id("already-in-results"))
    await _link(db, point_id("seed-1"), point_id("already-in-results"), strength=0.7)

    out = await graph_expansion.expand_neighbors(
        db,
        qdrant,
        [point_id("seed-1"), point_id("seed-2")],
        cap=10,
        exclude_ids=[point_id("already-in-results")],
    )

    assert [r.memory_id for r in out] == [point_id("nbr-1")]


async def test_expand_exclude_link_types_is_sql_level(db, qdrant):
    """A stronger excluded-type link must not consume the LIMIT budget.

    With ``cap=1`` and a stronger ``contradicts`` edge, a Python post-filter
    after ``LIMIT`` would return nothing; the SQL-level exclusion returns the
    ``supports`` neighbor.
    """
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("nbr-contra"))
    await _seed_memory(db, qdrant, point_id("nbr-support"))
    await _link(
        db, point_id("seed-1"), point_id("nbr-contra"), strength=0.95, link_type="contradicts"
    )
    await _link(db, point_id("seed-1"), point_id("nbr-support"), strength=0.4, link_type="supports")

    out = await graph_expansion.expand_neighbors(
        db,
        qdrant,
        [point_id("seed-1")],
        cap=1,
        exclude_link_types=("contradicts",),
    )

    assert [r.memory_id for r in out] == [point_id("nbr-support")]


async def test_expand_empty_seeds_returns_empty(db, qdrant):
    assert await graph_expansion.expand_neighbors(db, qdrant, [], cap=10) == []


async def test_expand_multi_seed_linked_from_collapses(db, qdrant):
    """A neighbor reached from several seeds appears ONCE, listing all seeds."""
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("seed-2"))
    await _seed_memory(db, qdrant, point_id("nbr-1"))
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.6)
    await _link(db, point_id("seed-2"), point_id("nbr-1"), strength=0.9)

    out = await graph_expansion.expand_neighbors(
        db, qdrant, [point_id("seed-1"), point_id("seed-2")], cap=10
    )

    assert len(out) == 1
    marker = out[0].payload["graph_expansion"]
    assert marker["linked_from"] == sorted([point_id("seed-1"), point_id("seed-2")])
    assert marker["strength"] == pytest.approx(0.9)  # MAX(strength)


# ─── maybe_expand: surface wrapper ───────────────────────────────────────────


async def _seed_linked_pair(db, qdrant) -> None:
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("nbr-1"))
    await _link(db, point_id("seed-1"), point_id("nbr-1"), strength=0.8)


async def _event_rows(db) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT event_type, subject_id, metrics_json FROM eval_events "
        "WHERE event_type LIKE 'graph_expansion_%'",
    )
    return [{"event_type": r[0], "subject_id": r[1], "metrics": json.loads(r[2])} for r in rows]


async def test_maybe_expand_off_is_passthrough_no_event(db, qdrant, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "off"}})
    await _seed_linked_pair(db, qdrant)
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")

    assert out is results
    assert await _event_rows(db) == []


async def test_maybe_expand_shadow_returns_unchanged_and_emits(db, qdrant, config_dirs):
    await _seed_linked_pair(db, qdrant)  # defaults: mode=shadow
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(
        db,
        qdrant,
        results,
        surface="full",
        recall_event_id="evt-123",
    )

    assert [r.memory_id for r in out] == [point_id("seed-1")]
    events = await _event_rows(db)
    assert len(events) == 1
    assert events[0]["event_type"] == "graph_expansion_shadow"
    assert events[0]["subject_id"] == "evt-123"
    m = events[0]["metrics"]
    assert m["surface"] == "full"
    assert m["seed_count"] == 1
    assert m["neighbors_returned"] == 1
    assert m["neighbor_ids"] == [point_id("nbr-1")]
    assert "latency_ms" in m


async def test_maybe_expand_live_appends_neighbors_and_emits(db, qdrant, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_linked_pair(db, qdrant)
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")

    assert [r.memory_id for r in out] == [point_id("seed-1"), point_id("nbr-1")]
    assert out[1].payload["graph_expansion"]["linked_from"] == [point_id("seed-1")]
    events = await _event_rows(db)
    assert len(events) == 1
    assert events[0]["event_type"] == "graph_expansion_live"


async def test_maybe_expand_proactive_uses_proactive_cap(db, qdrant, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_memory(db, qdrant, point_id("seed-1"))
    for i, s in enumerate((0.9, 0.8, 0.7)):
        await _seed_memory(db, qdrant, point_id(f"nbr-{i}"))
        await _link(db, point_id("seed-1"), point_id(f"nbr-{i}"), strength=s)
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="proactive")

    # default proactive_max_neighbors == 2
    assert [r.memory_id for r in out] == [point_id("seed-1"), point_id("nbr-0"), point_id("nbr-1")]


async def test_maybe_expand_excludes_contradicts_by_default(db, qdrant, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("nbr-contra"))
    await _link(
        db, point_id("seed-1"), point_id("nbr-contra"), strength=0.9, link_type="contradicts"
    )
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")

    assert [r.memory_id for r in out] == [point_id("seed-1")]


async def test_maybe_expand_empty_results_is_passthrough_no_event(db, qdrant, config_dirs):
    out = await graph_expansion.maybe_expand(db, qdrant, [], surface="full")
    assert out == []
    assert await _event_rows(db) == []


async def test_maybe_expand_metric_failure_never_raises(db, qdrant, config_dirs, monkeypatch):
    await _seed_linked_pair(db, qdrant)

    async def _boom(*args, **kwargs):
        raise RuntimeError("eval_events unavailable")

    monkeypatch.setattr(graph_expansion.j9_eval, "insert_event", _boom)
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")

    assert [r.memory_id for r in out] == [point_id("seed-1")]  # shadow: unchanged


async def test_maybe_expand_expansion_failure_never_raises(db, qdrant, config_dirs, monkeypatch):
    """A broken expansion query must never break recall itself."""
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(graph_expansion, "expand_neighbors", _boom)
    results = [_result(point_id("seed-1"))]

    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")

    assert [r.memory_id for r in out] == [point_id("seed-1")]


async def test_maybe_expand_malformed_config_values_never_raise(db, qdrant, config_dirs):
    """Config extraction is inside the best-effort guard: a hand-edited
    non-iterable exclude_link_types or non-int cap must degrade (results
    unchanged), never crash the recall surface (compact/proactive have no
    outer guard)."""
    base, _ = config_dirs
    await _seed_linked_pair(db, qdrant)
    results = [_result(point_id("seed-1"))]

    _write(base, {"graph_expansion": {"mode": "live", "exclude_link_types": 5}})
    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")
    assert [r.memory_id for r in out] == [point_id("seed-1")]

    _write(base, {"graph_expansion": {"mode": "live", "max_neighbors": "ten"}})
    out = await graph_expansion.maybe_expand(db, qdrant, results, surface="full")
    assert [r.memory_id for r in out] == [point_id("seed-1")]


async def test_expand_skips_expired_neighbor(db, qdrant):
    """Bitemporal parity with normal recall (Codex #1069 P2): a neighbor whose
    invalid_at has passed must not resurface via expansion."""
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("nbr-live"))
    await _seed_memory(db, qdrant, point_id("nbr-expired"))
    await db.execute(
        "UPDATE memory_metadata SET invalid_at = '2020-01-01T00:00:00+00:00' WHERE memory_id = ?",
        (point_id("nbr-expired"),),
    )
    await db.commit()
    await _link(db, point_id("seed-1"), point_id("nbr-expired"), strength=0.9)
    await _link(db, point_id("seed-1"), point_id("nbr-live"), strength=0.5)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=10)

    assert [r.memory_id for r in out] == [point_id("nbr-live")]


async def test_expand_skips_deprecated_neighbor(db, qdrant):
    """Deprecated (superseded) memories are filtered by search_ranked — the
    expansion path must not readmit them."""
    await _seed_memory(db, qdrant, point_id("seed-1"))
    await _seed_memory(db, qdrant, point_id("nbr-dep"))
    await db.execute(
        "UPDATE memory_metadata SET deprecated = 1 WHERE memory_id = ?",
        (point_id("nbr-dep"),),
    )
    await db.commit()
    await _link(db, point_id("seed-1"), point_id("nbr-dep"), strength=0.9)

    out = await graph_expansion.expand_neighbors(db, qdrant, [point_id("seed-1")], cap=10)

    assert out == []


# ─── hydrate_neighbours: id-keyed reads against a real Qdrant client ─────────
# Qdrant point ids are UUIDs (or ints), so these tests use UUIDs.

ALIVE = "00000000-0000-4000-8000-000000000001"
FUTURE = "00000000-0000-4000-8000-000000000002"
NO_META = "00000000-0000-4000-8000-000000000003"
GHOST = "00000000-0000-4000-8000-000000000004"
ABSENT = "00000000-0000-4000-8000-000000000005"
KEYWORD_ONLY = "00000000-0000-4000-8000-000000000006"
PENDING = "00000000-0000-4000-8000-000000000007"
FAILED = "00000000-0000-4000-8000-000000000008"


@pytest.fixture
def real_qdrant():
    from qdrant_client import QdrantClient, models

    client = QdrantClient(location=":memory:")
    for name in ("episodic_memory", "knowledge_base"):
        client.create_collection(
            name,
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
    yield client
    client.close()


def _put_point(client, collection: str, memory_id: str, **payload) -> None:
    from qdrant_client import models

    client.upsert(
        collection,
        points=[models.PointStruct(id=memory_id, vector=[0.1, 0.2, 0.3, 0.4], payload=payload)],
    )


async def _metadata(db, memory_id: str, *, collection: str = "episodic_memory", **kw) -> None:
    await memory_crud.create_metadata(
        db, memory_id=memory_id, created_at="2026-07-01T00:00:00Z", collection=collection, **kw
    )


async def test_hydrate_neighbours_reads_points_and_metadata(db, real_qdrant):
    """Text comes from the point, visibility and origin from memory_metadata,
    and nothing from the FTS index."""
    # The metadata collection column is unreliable: this point lives in the
    # knowledge base whatever the column says. Its payload still says
    # deprecated, as after a dream-cycle rollback whose payload write failed
    # (metadata is restored first); metadata, the authoritative copy, says 0.
    await _metadata(db, ALIVE, origin_class="external_untrusted")
    _put_point(
        real_qdrant,
        "knowledge_base",
        ALIVE,
        content="kb text",
        tags=["foo", "bar"],
        source_type="memory",
        deprecated=True,
    )
    await _metadata(db, FUTURE, invalid_at="2099-01-01T00:00:00+00:00")
    # Deprecated after its point was written: the payload copy still says
    # False, and metadata, the authoritative copy, says 1.
    await db.execute("UPDATE memory_metadata SET deprecated = 1 WHERE memory_id = ?", (FUTURE,))
    _put_point(
        real_qdrant,
        "episodic_memory",
        FUTURE,
        content="later",
        source_type="memory",
        deprecated=False,
    )
    # A point with no metadata row is a deleted memory's leftover vector: its
    # payload still holds the deleted text, and recall drops it as a ghost.
    _put_point(real_qdrant, "episodic_memory", NO_META, content="deleted", source_type="m")
    # An embedded memory whose point is gone is a dangling link, even when a
    # stale FTS row survives it.
    await _metadata(db, GHOST)
    await memory_crud.create(db, memory_id=GHOST, content="stale fts row")
    await db.commit()

    got = await graph_expansion.hydrate_neighbours(
        db, real_qdrant, [ALIVE, FUTURE, NO_META, GHOST, ABSENT]
    )

    assert got == {
        ALIVE: {
            "content": "kb text",
            "source_type": "memory",
            "tags": "foo bar",  # the FTS spelling of the payload's tag list
            "collection": "knowledge_base",
            "origin_class": "external_untrusted",
            "invalid_at": None,
            "deprecated": 0,
        },
        FUTURE: {
            "content": "later",
            "source_type": "memory",
            "tags": "",
            "collection": "episodic_memory",
            "origin_class": None,
            "invalid_at": "2099-01-01T00:00:00+00:00",
            "deprecated": 1,
        },
    }
    assert await graph_expansion.hydrate_neighbours(db, real_qdrant, []) == {}


async def test_hydrate_neighbours_reads_never_embedded_memories_from_fts(db, real_qdrant):
    """A memory that was never embedded has no point, and FTS holds its only
    text: it stays a neighbour, as before #5052, with visibility from
    memory_metadata. A subsystem write is FTS-only for good; a pending or
    failed one has no vector yet."""
    for mid, status in ((KEYWORD_ONLY, "fts5_only"), (PENDING, "pending"), (FAILED, "failed")):
        await _metadata(db, mid, embedding_status=status)
        await memory_crud.create(
            db,
            memory_id=mid,
            content=f"{status} text",
            source_type="memory",
            tags="a b",
            collection="knowledge_base",
        )
    await db.execute(
        "UPDATE memory_metadata SET deprecated = 1, origin_class = 'external_untrusted', "
        "invalid_at = '2099-01-01T00:00:00+00:00' WHERE memory_id = ?",
        (PENDING,),
    )
    # A neighbour with a point, in the same call: it keeps the point path.
    await _metadata(db, ALIVE)
    _put_point(real_qdrant, "episodic_memory", ALIVE, content="pointed", source_type="memory")
    await db.commit()

    got = await graph_expansion.hydrate_neighbours(
        db, real_qdrant, [ALIVE, KEYWORD_ONLY, PENDING, FAILED]
    )

    assert set(got) == {ALIVE, KEYWORD_ONLY, PENDING, FAILED}
    assert got[ALIVE]["content"] == "pointed"
    assert got[KEYWORD_ONLY] == {
        "content": "fts5_only text",
        "source_type": "memory",
        "tags": "a b",
        "collection": "knowledge_base",  # from the FTS row, as before #5052
        "origin_class": None,
        "invalid_at": None,
        "deprecated": 0,
    }
    # memory_metadata, not FTS, decides visibility and origin
    assert got[PENDING]["deprecated"] == 1
    assert got[PENDING]["origin_class"] == "external_untrusted"
    assert got[PENDING]["invalid_at"] == "2099-01-01T00:00:00+00:00"


async def test_hydrate_neighbours_skips_the_fts_scan_when_every_neighbour_has_a_point(
    db, real_qdrant, monkeypatch
):
    """The FTS read is the one scan left, so it runs only for a never-embedded
    neighbour; an embedded memory whose point is gone stays dangling."""
    await _metadata(db, ALIVE)
    _put_point(real_qdrant, "episodic_memory", ALIVE, content="text", source_type="memory")
    await _metadata(db, GHOST)
    await memory_crud.create(db, memory_id=GHOST, content="stale fts row")
    await db.commit()
    asked: list[list[str]] = []
    real = memory_crud.expansion_fts_rows_by_ids

    async def spy(conn, ids):
        asked.append(list(ids))
        return await real(conn, ids)

    monkeypatch.setattr(memory_crud, "expansion_fts_rows_by_ids", spy)

    got = await graph_expansion.hydrate_neighbours(db, real_qdrant, [ALIVE, GHOST])

    assert set(got) == {ALIVE}
    assert asked == []  # FTS is never read


async def test_hydrate_neighbours_point_in_both_collections_reads_as_knowledge_base(
    db, real_qdrant
):
    await _metadata(db, ALIVE)
    await db.commit()
    _put_point(real_qdrant, "episodic_memory", ALIVE, content="ep", source_type="memory")
    _put_point(real_qdrant, "knowledge_base", ALIVE, content="kb", source_type="memory")

    got = await graph_expansion.hydrate_neighbours(db, real_qdrant, [ALIVE])

    assert got[ALIVE]["collection"] == "knowledge_base"
    assert got[ALIVE]["content"] == "kb"


async def test_hydrate_neighbours_ignores_points_not_asked_for(db):
    class _ExtraPoint:
        def retrieve(self, collection_name, ids, with_payload, with_vectors):
            return [SimpleNamespace(id=ABSENT, payload={"content": "not asked for"})]

    # A real memory, so only the asked-for filter can keep it out.
    await _metadata(db, ABSENT)
    await db.commit()
    assert await graph_expansion.hydrate_neighbours(db, _ExtraPoint(), [ALIVE]) == {}


async def test_hydrate_neighbours_drops_ids_that_cannot_be_points(db, qdrant):
    """A Qdrant server rejects a whole retrieve that names a non-UUID id (the
    fake does too), so one malformed link must not cost the other neighbours."""
    await _metadata(db, ALIVE)
    await db.commit()
    qdrant.put("episodic_memory", ALIVE, content="kept", source_type="memory", tags=[])
    with pytest.raises(ValueError):
        qdrant.retrieve(collection_name="episodic_memory", ids=[ALIVE, "ghost-no-such-memory"])

    got = await graph_expansion.hydrate_neighbours(db, qdrant, [ALIVE, "ghost-no-such-memory"])

    assert set(got) == {ALIVE}
    assert await graph_expansion.hydrate_neighbours(db, qdrant, ["not-a-uuid"]) == {}


async def test_hydrate_neighbours_raises_when_the_point_read_fails(db):
    """No silent fallback to the FTS scan: the callers' guards log it and skip
    expansion for that recall."""

    class _Unreachable:
        def retrieve(self, **kwargs):
            raise ConnectionError("qdrant unreachable")

    with pytest.raises(ConnectionError):
        await graph_expansion.hydrate_neighbours(db, _Unreachable(), [ALIVE])


async def test_hydrate_neighbours_reads_points_off_the_event_loop(db, real_qdrant):
    """The Qdrant client is synchronous, so the point read runs in a worker
    thread, never on the loop that serves every other recall."""
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real_retrieve = real_qdrant.retrieve

    def _spy(**kwargs):
        seen.append(threading.current_thread())
        return real_retrieve(**kwargs)

    real_qdrant.retrieve = _spy
    _put_point(real_qdrant, "episodic_memory", ALIVE, content="x", source_type="memory")

    await graph_expansion.hydrate_neighbours(db, real_qdrant, [ALIVE])

    assert seen
    assert all(t is not loop_thread for t in seen)


# --- reranker mode + kill switch (MCP recall tool gate) --------------------
# reranker_enabled() gates Voyage reranking on the MCP recall tools ONLY.
# off | live (no shadow), DEFAULT live. Kill = config mode off OR the
# GENESIS_MEMORY_RERANK_OFF env. Master ``enabled: false`` also forces off.


def test_reranker_defaults_to_live_with_no_files(config_dirs, monkeypatch):
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    cfg = graph_expansion.load_recall_config()
    assert cfg["reranker"]["mode"] == "live"
    assert graph_expansion.reranker_enabled() is True


def test_reranker_mode_off_via_base(config_dirs, monkeypatch):
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    base, _ = config_dirs
    _write(base, {"reranker": {"mode": "off"}})
    assert graph_expansion.reranker_enabled() is False


def test_reranker_overlay_wins_over_base(config_dirs, monkeypatch):
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    base, overlay = config_dirs
    _write(base, {"reranker": {"mode": "live"}})
    _write(overlay, {"reranker": {"mode": "off"}})
    assert graph_expansion.reranker_enabled() is False


def test_reranker_master_enabled_false_forces_off(config_dirs, monkeypatch):
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    base, _ = config_dirs
    _write(base, {"enabled": False, "reranker": {"mode": "live"}})
    assert graph_expansion.reranker_enabled() is False


def test_reranker_invalid_mode_degrades_to_live(config_dirs, monkeypatch):
    # Unlike graph_expansion (degrades to shadow), the reranker has no shadow —
    # an invalid value keeps reranking ON (the tools promise it).
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    base, _ = config_dirs
    _write(base, {"reranker": {"mode": "banana"}})
    assert graph_expansion.reranker_enabled() is True


def test_reranker_unquoted_off_boolean_is_off(config_dirs, monkeypatch):
    # A hand-edited unquoted ``mode: off`` parses as YAML-1.1 boolean False.
    monkeypatch.delenv("GENESIS_MEMORY_RERANK_OFF", raising=False)
    base, _ = config_dirs
    base.write_text("reranker:\n  mode: off\n")
    assert graph_expansion.reranker_enabled() is False


def test_reranker_env_kill_forces_off_even_when_config_live(config_dirs, monkeypatch):
    base, _ = config_dirs
    _write(base, {"reranker": {"mode": "live"}})
    monkeypatch.setenv("GENESIS_MEMORY_RERANK_OFF", "1")
    assert graph_expansion.reranker_enabled() is False


def test_reranker_env_kill_various_truthy(config_dirs, monkeypatch):
    for val in ("1", "true", "yes"):
        monkeypatch.setenv("GENESIS_MEMORY_RERANK_OFF", val)
        assert graph_expansion.reranker_enabled() is False
    monkeypatch.setenv("GENESIS_MEMORY_RERANK_OFF", "0")
    assert graph_expansion.reranker_enabled() is True
