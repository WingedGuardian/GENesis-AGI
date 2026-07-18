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
from pathlib import Path

import pytest
import yaml

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.memory import graph_expansion
from genesis.memory.types import RetrievalResult


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


async def _seed_memory(
    db,
    memory_id: str,
    *,
    content: str | None = None,
    collection: str = "episodic_memory",
    origin_class: str | None = "owner",
) -> None:
    await memory_crud.create(
        db,
        memory_id=memory_id,
        content=content if content is not None else f"content of {memory_id}",
        source_type="memory",
        collection=collection,
    )
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


def _result(memory_id: str = "seed-1") -> RetrievalResult:
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


async def test_expand_returns_stored_provenance_and_marker(db):
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-1", collection="knowledge_base", origin_class="external_untrusted")
    await _link(db, "seed-1", "nbr-1", strength=0.9)

    out = await graph_expansion.expand_neighbors(db, ["seed-1"], cap=10)

    assert len(out) == 1
    r = out[0]
    assert isinstance(r, RetrievalResult)
    assert r.memory_id == "nbr-1"
    assert r.content == "content of nbr-1"
    # stored-first provenance: never synthetic values
    assert r.collection == "knowledge_base"
    assert r.origin_class == "external_untrusted"
    assert r.source_pipeline is None  # no SQLite column; never fabricated
    # sorts after any organic result
    assert r.score == pytest.approx(0.01 * 0.9)
    marker = r.payload["graph_expansion"]
    assert marker["linked_from"] == ["seed-1"]
    assert marker["strength"] == pytest.approx(0.9)


async def test_expand_skips_dangling_neighbors(db):
    await _seed_memory(db, "seed-1")
    # link rows exist but the neighbor memory does not resolve
    await _link(db, "seed-1", "ghost-1", strength=0.9)
    await _seed_memory(db, "nbr-1")
    await _link(db, "seed-1", "nbr-1", strength=0.5)

    out = await graph_expansion.expand_neighbors(db, ["seed-1"], cap=10)

    assert [r.memory_id for r in out] == ["nbr-1"]


async def test_expand_cap_and_strength_ordering(db):
    await _seed_memory(db, "seed-1")
    for i, s in enumerate((0.3, 0.9, 0.6)):
        await _seed_memory(db, f"nbr-{i}")
        await _link(db, "seed-1", f"nbr-{i}", strength=s)

    out = await graph_expansion.expand_neighbors(db, ["seed-1"], cap=2)

    assert [r.memory_id for r in out] == ["nbr-1", "nbr-2"]  # 0.9, 0.6


async def test_expand_never_returns_seeds_or_excluded(db):
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "seed-2")
    await _seed_memory(db, "nbr-1")
    await _link(db, "seed-1", "seed-2", strength=0.9)  # seed↔seed link
    await _link(db, "seed-1", "nbr-1", strength=0.8)
    await _seed_memory(db, "already-in-results")
    await _link(db, "seed-1", "already-in-results", strength=0.7)

    out = await graph_expansion.expand_neighbors(
        db,
        ["seed-1", "seed-2"],
        cap=10,
        exclude_ids=["already-in-results"],
    )

    assert [r.memory_id for r in out] == ["nbr-1"]


async def test_expand_exclude_link_types_is_sql_level(db):
    """A stronger excluded-type link must not consume the LIMIT budget.

    With ``cap=1`` and a stronger ``contradicts`` edge, a Python post-filter
    after ``LIMIT`` would return nothing; the SQL-level exclusion returns the
    ``supports`` neighbor.
    """
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-contra")
    await _seed_memory(db, "nbr-support")
    await _link(db, "seed-1", "nbr-contra", strength=0.95, link_type="contradicts")
    await _link(db, "seed-1", "nbr-support", strength=0.4, link_type="supports")

    out = await graph_expansion.expand_neighbors(
        db,
        ["seed-1"],
        cap=1,
        exclude_link_types=("contradicts",),
    )

    assert [r.memory_id for r in out] == ["nbr-support"]


async def test_expand_empty_seeds_returns_empty(db):
    assert await graph_expansion.expand_neighbors(db, [], cap=10) == []


async def test_expand_multi_seed_linked_from_collapses(db):
    """A neighbor reached from several seeds appears ONCE, listing all seeds."""
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "seed-2")
    await _seed_memory(db, "nbr-1")
    await _link(db, "seed-1", "nbr-1", strength=0.6)
    await _link(db, "seed-2", "nbr-1", strength=0.9)

    out = await graph_expansion.expand_neighbors(db, ["seed-1", "seed-2"], cap=10)

    assert len(out) == 1
    marker = out[0].payload["graph_expansion"]
    assert sorted(marker["linked_from"]) == ["seed-1", "seed-2"]
    assert marker["strength"] == pytest.approx(0.9)  # MAX(strength)


# ─── maybe_expand: surface wrapper ───────────────────────────────────────────


async def _seed_linked_pair(db) -> None:
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-1")
    await _link(db, "seed-1", "nbr-1", strength=0.8)


async def _event_rows(db) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT event_type, subject_id, metrics_json FROM eval_events "
        "WHERE event_type LIKE 'graph_expansion_%'",
    )
    return [{"event_type": r[0], "subject_id": r[1], "metrics": json.loads(r[2])} for r in rows]


async def test_maybe_expand_off_is_passthrough_no_event(db, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "off"}})
    await _seed_linked_pair(db)
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="full")

    assert out is results
    assert await _event_rows(db) == []


async def test_maybe_expand_shadow_returns_unchanged_and_emits(db, config_dirs):
    await _seed_linked_pair(db)  # defaults: mode=shadow
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(
        db,
        results,
        surface="full",
        recall_event_id="evt-123",
    )

    assert [r.memory_id for r in out] == ["seed-1"]
    events = await _event_rows(db)
    assert len(events) == 1
    assert events[0]["event_type"] == "graph_expansion_shadow"
    assert events[0]["subject_id"] == "evt-123"
    m = events[0]["metrics"]
    assert m["surface"] == "full"
    assert m["seed_count"] == 1
    assert m["neighbors_returned"] == 1
    assert m["neighbor_ids"] == ["nbr-1"]
    assert "latency_ms" in m


async def test_maybe_expand_live_appends_neighbors_and_emits(db, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_linked_pair(db)
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="full")

    assert [r.memory_id for r in out] == ["seed-1", "nbr-1"]
    assert out[1].payload["graph_expansion"]["linked_from"] == ["seed-1"]
    events = await _event_rows(db)
    assert len(events) == 1
    assert events[0]["event_type"] == "graph_expansion_live"


async def test_maybe_expand_proactive_uses_proactive_cap(db, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_memory(db, "seed-1")
    for i, s in enumerate((0.9, 0.8, 0.7)):
        await _seed_memory(db, f"nbr-{i}")
        await _link(db, "seed-1", f"nbr-{i}", strength=s)
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="proactive")

    # default proactive_max_neighbors == 2
    assert [r.memory_id for r in out] == ["seed-1", "nbr-0", "nbr-1"]


async def test_maybe_expand_excludes_contradicts_by_default(db, config_dirs):
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-contra")
    await _link(db, "seed-1", "nbr-contra", strength=0.9, link_type="contradicts")
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="full")

    assert [r.memory_id for r in out] == ["seed-1"]


async def test_maybe_expand_empty_results_is_passthrough_no_event(db, config_dirs):
    out = await graph_expansion.maybe_expand(db, [], surface="full")
    assert out == []
    assert await _event_rows(db) == []


async def test_maybe_expand_metric_failure_never_raises(db, config_dirs, monkeypatch):
    await _seed_linked_pair(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("eval_events unavailable")

    monkeypatch.setattr(graph_expansion.j9_eval, "insert_event", _boom)
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="full")

    assert [r.memory_id for r in out] == ["seed-1"]  # shadow: unchanged


async def test_maybe_expand_expansion_failure_never_raises(db, config_dirs, monkeypatch):
    """A broken expansion query must never break recall itself."""
    base, _ = config_dirs
    _write(base, {"graph_expansion": {"mode": "live"}})

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(graph_expansion, "expand_neighbors", _boom)
    results = [_result("seed-1")]

    out = await graph_expansion.maybe_expand(db, results, surface="full")

    assert [r.memory_id for r in out] == ["seed-1"]


async def test_maybe_expand_malformed_config_values_never_raise(db, config_dirs):
    """Config extraction is inside the best-effort guard: a hand-edited
    non-iterable exclude_link_types or non-int cap must degrade (results
    unchanged), never crash the recall surface (compact/proactive have no
    outer guard)."""
    base, _ = config_dirs
    await _seed_linked_pair(db)
    results = [_result("seed-1")]

    _write(base, {"graph_expansion": {"mode": "live", "exclude_link_types": 5}})
    out = await graph_expansion.maybe_expand(db, results, surface="full")
    assert [r.memory_id for r in out] == ["seed-1"]

    _write(base, {"graph_expansion": {"mode": "live", "max_neighbors": "ten"}})
    out = await graph_expansion.maybe_expand(db, results, surface="full")
    assert [r.memory_id for r in out] == ["seed-1"]


async def test_expand_skips_expired_neighbor(db):
    """Bitemporal parity with normal recall (Codex #1069 P2): a neighbor whose
    invalid_at has passed must not resurface via expansion."""
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-live")
    await _seed_memory(db, "nbr-expired")
    await db.execute(
        "UPDATE memory_metadata SET invalid_at = '2020-01-01T00:00:00+00:00' "
        "WHERE memory_id = 'nbr-expired'",
    )
    await db.commit()
    await _link(db, "seed-1", "nbr-expired", strength=0.9)
    await _link(db, "seed-1", "nbr-live", strength=0.5)

    out = await graph_expansion.expand_neighbors(db, ["seed-1"], cap=10)

    assert [r.memory_id for r in out] == ["nbr-live"]


async def test_expand_skips_deprecated_neighbor(db):
    """Deprecated (superseded) memories are filtered by search_ranked — the
    expansion path must not readmit them."""
    await _seed_memory(db, "seed-1")
    await _seed_memory(db, "nbr-dep")
    await db.execute(
        "UPDATE memory_metadata SET deprecated = 1 WHERE memory_id = 'nbr-dep'",
    )
    await db.commit()
    await _link(db, "seed-1", "nbr-dep", strength=0.9)

    out = await graph_expansion.expand_neighbors(db, ["seed-1"], cap=10)

    assert out == []
