"""Importance shield — percentile helper, state computation, cluster filtering,
drain-time re-check. All install-agnostic (real db fixture, no live services)."""

from __future__ import annotations

from datetime import datetime, timedelta

from genesis.memory import dream_shield, dream_shield_config

NOW = "2026-08-05T00:00:00+00:00"


def _iso_days_ago(n: int) -> str:
    return (datetime.fromisoformat(NOW) - timedelta(days=n)).isoformat()


def _point(mid, *, confidence=0.5, retrieved_count=0, days_old=10, **extra):
    payload = {
        "confidence": confidence,
        "retrieved_count": retrieved_count,
        "created_at": _iso_days_ago(days_old),
        "source": "session_extraction",
        **extra,
    }
    return {"id": mid, "payload": payload}


# ── percentile helper ─────────────────────────────────────────────────────────


def test_percentile_empty_is_none():
    assert dream_shield._percentile([], 0.9) is None


def test_percentile_single_value():
    assert dream_shield._percentile([0.4], 0.9) == 0.4


def test_percentile_p90_top_decile():
    # 10 values → p0.9 threshold is the max (top 10% = 1 element).
    vals = [float(i) for i in range(10)]  # 0..9
    assert dream_shield._percentile(vals, 0.9) == 9.0


def test_percentile_p80_top_quintile():
    vals = [float(i) for i in range(10)]  # 0..9
    # p0.8 → index int(0.8*10)=8 → value 8; members >= 8 are {8,9} = top 20%.
    assert dream_shield._percentile(vals, 0.8) == 8.0


def test_percentile_unsorted_input():
    assert dream_shield._percentile([5.0, 1.0, 9.0, 3.0, 7.0], 0.0) == 1.0


# ── compute_shield_state ──────────────────────────────────────────────────────


def _enable(monkeypatch, **overrides):
    cfg = {
        "enabled": True,
        "activation_percentile": 0.90,
        "centrality_percentile": 0.90,
        "confidence_floor": 0.98,
        "deprecated_edge_prune_days": 30,
        **overrides,
    }
    monkeypatch.setattr(dream_shield_config, "load_config", lambda: cfg)
    monkeypatch.setattr(dream_shield_config, "shield_enabled", lambda: cfg["enabled"])


async def test_compute_shield_state_none_when_disabled(monkeypatch, db):
    monkeypatch.setattr(dream_shield_config, "shield_enabled", lambda: False)
    state = await dream_shield.compute_shield_state(db, [_point("a")], now=NOW)
    assert state is None


async def test_compute_shield_state_empty_points(monkeypatch, db):
    _enable(monkeypatch)
    state = await dream_shield.compute_shield_state(db, [], now=NOW)
    assert state is not None
    assert state.population == 0
    assert state.activation_threshold is None  # nothing to threshold


async def test_compute_shield_state_empty_centrality_cache(monkeypatch, db):
    _enable(monkeypatch)
    points = [_point(f"m{i}", retrieved_count=i, days_old=1) for i in range(10)]
    state = await dream_shield.compute_shield_state(db, points, now=NOW)
    assert state.centrality_threshold is None  # cache empty → activation-only
    assert state.activation_threshold is not None
    assert state.population == 10


async def test_compute_shield_state_reads_nonzero_centrality(monkeypatch, db):
    _enable(monkeypatch)
    now_iso = NOW
    await db.executemany(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) VALUES (?,?,?)",
        [("bridge", 0.5, now_iso), ("mid", 0.2, now_iso), ("low", 0.01, now_iso)],
    )
    await db.commit()
    points = [_point("bridge"), _point("mid"), _point("low")]
    state = await dream_shield.compute_shield_state(db, points, now=NOW)
    assert state.centrality_threshold is not None
    assert state.centrality_by_id["bridge"] == 0.5


async def test_high_retrieved_high_confidence_lands_above_threshold(monkeypatch, db):
    _enable(monkeypatch)
    # One clearly-salient point (recent, high confidence, heavily retrieved)
    # among many dull ones with a realistic spread (increasing age → decreasing
    # activation) → the star is shielded by activation; the dullest is not.
    dull = [_point(f"d{i}", confidence=0.4, retrieved_count=0, days_old=100 + i) for i in range(20)]
    star = _point("star", confidence=0.95, retrieved_count=15, days_old=1)
    state = await dream_shield.compute_shield_state(db, [*dull, star], now=NOW)
    assert dream_shield._member_is_shielded(star, state) is True
    # dull[-1] is the oldest → lowest activation → well below the p90 bar.
    assert dream_shield._member_is_shielded(dull[-1], state) is False


# ── apply_shield_to_clusters ──────────────────────────────────────────────────


async def test_compute_shield_state_survives_malformed_created_at(monkeypatch, db):
    """One point with a garbage created_at must not crash the whole shield —
    it gets activation 0.0 (not shielded by activation) while the rest score."""
    _enable(monkeypatch)
    good = [_point(f"g{i}", retrieved_count=i, days_old=1) for i in range(5)]
    bad = {
        "id": "bad",
        "payload": {
            "confidence": 0.5,
            "created_at": "not-a-date",
            "retrieved_count": 0,
            "source": "session_extraction",
        },
    }
    state = await dream_shield.compute_shield_state(db, [*good, bad], now=NOW)
    assert state is not None
    assert state.population == 6
    assert state.activation_by_id["bad"] == 0.0  # degraded, not crashed


async def test_shield_filter_live_survives_malformed_created_at(monkeypatch, db):
    """A malformed live payload at drain must not break the drain."""
    _enable(monkeypatch)
    bad = {"id": "bad", "payload": {"confidence": 0.4, "created_at": "xyz"}}
    a, b = _point("a", confidence=0.4), _point("b", confidence=0.4)
    survivors, n = await dream_shield.shield_filter_live(
        db, [bad, a, b], activation_threshold=0.30, centrality_threshold=None, now=NOW
    )
    assert n == 0  # bad point degrades to activation 0.0, not shielded
    assert {m["id"] for m in survivors} == {"bad", "a", "b"}


async def test_compute_shield_state_survives_null_confidence(monkeypatch, db):
    """A payload with explicit `confidence: null` must not crash the floor
    comparison (`.get(k, default)` returns None when the key is present-but-null,
    and None >= float raises TypeError)."""
    _enable(monkeypatch)
    # Recent, high-activation neighbours set the bar; the null-confidence member
    # is old (low activation) so only the confidence floor could shield it.
    good = [_point(f"g{i}", retrieved_count=10, days_old=1) for i in range(3)]
    nullc = {
        "id": "nc",
        "payload": {
            "confidence": None,
            "created_at": _iso_days_ago(300),
            "retrieved_count": 0,
            "source": "session_extraction",
        },
    }
    state = await dream_shield.compute_shield_state(db, [*good, nullc], now=NOW)
    assert state is not None
    # Must not raise; coerced to the 0.5 default → below the 0.98 floor, and old
    # → below the activation bar → not shielded.
    assert dream_shield._member_is_shielded(nullc, state) is False


async def test_shield_filter_live_survives_null_confidence(monkeypatch, db):
    """Drain-time floor comparison must not crash on null confidence — otherwise
    it propagates AFTER the item is marked processing and stalls the drain."""
    _enable(monkeypatch)
    nullc = {"id": "nc", "payload": {"confidence": None, "created_at": _iso_days_ago(1)}}
    a, b = _point("a", confidence=0.4), _point("b", confidence=0.4)
    survivors, n = await dream_shield.shield_filter_live(
        db, [nullc, a, b], activation_threshold=0.30, centrality_threshold=None, now=NOW
    )
    assert n == 0
    assert {m["id"] for m in survivors} == {"nc", "a", "b"}


async def test_centrality_percentile_excludes_out_of_population(monkeypatch, db):
    """The centrality percentile must be computed over the LIVE population only.
    A high-centrality deprecated node still in the cache (dream soft-deletes keep
    metadata/links) must not inflate the threshold and under-shield live bridges."""
    _enable(monkeypatch, centrality_percentile=0.5)
    await db.executemany(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) VALUES (?,?,?)",
        [("ghost", 0.99, NOW), ("m0", 0.01, NOW), ("m1", 0.02, NOW), ("m2", 0.03, NOW)],
    )
    await db.commit()
    points = [_point("m0"), _point("m1"), _point("m2")]  # 'ghost' NOT in population
    state = await dream_shield.compute_shield_state(db, points, now=NOW)
    # p0.5 over the in-population {0.01,0.02,0.03} = 0.02; if 'ghost' 0.99 leaked
    # in it would be 0.03. Also 'ghost' must not appear in the by_id map.
    assert state.centrality_threshold == 0.02
    assert "ghost" not in state.centrality_by_id


async def test_apply_shield_none_state_is_noop():
    clusters = [[_point("a"), _point("b")]]
    out, stats = dream_shield.apply_shield_to_clusters(clusters, None)
    assert out == clusters
    assert stats == {"members_shielded": 0, "clusters_trimmed": 0, "clusters_dropped": 0}


async def test_apply_shield_trims_shielded_member(monkeypatch, db):
    _enable(monkeypatch)
    star = _point("star", confidence=0.99)  # shielded by confidence floor
    a, b = _point("a", confidence=0.4), _point("b", confidence=0.4)
    state = await dream_shield.compute_shield_state(db, [star, a, b], now=NOW)
    out, stats = dream_shield.apply_shield_to_clusters([[star, a, b]], state)
    assert len(out) == 1
    assert {m["id"] for m in out[0]} == {"a", "b"}  # star removed, ≥2 survive
    assert stats["members_shielded"] == 1
    assert stats["clusters_trimmed"] == 1
    assert stats["clusters_dropped"] == 0


async def test_apply_shield_drops_cluster_below_two(monkeypatch, db):
    _enable(monkeypatch)
    star = _point("star", confidence=0.99)
    a = _point("a", confidence=0.4)
    state = await dream_shield.compute_shield_state(db, [star, a], now=NOW)
    out, stats = dream_shield.apply_shield_to_clusters([[star, a]], state)
    assert out == []  # only 1 survivor → whole cluster dropped
    assert stats["members_shielded"] == 1
    assert stats["clusters_dropped"] == 1


async def test_apply_shield_all_members_shielded(monkeypatch, db):
    _enable(monkeypatch)
    s1, s2 = _point("s1", confidence=0.99), _point("s2", confidence=0.99)
    state = await dream_shield.compute_shield_state(db, [s1, s2], now=NOW)
    out, stats = dream_shield.apply_shield_to_clusters([[s1, s2]], state)
    assert out == []
    assert stats["members_shielded"] == 2
    assert stats["clusters_dropped"] == 1


# ── shield_filter_live (drain-time) ───────────────────────────────────────────


async def test_filter_live_confidence_floor(monkeypatch, db):
    _enable(monkeypatch)
    star = _point("star", confidence=0.99)
    a, b = _point("a", confidence=0.4), _point("b", confidence=0.4)
    survivors, n = await dream_shield.shield_filter_live(
        db, [star, a, b], activation_threshold=999.0, centrality_threshold=None, now=NOW
    )
    assert {m["id"] for m in survivors} == {"a", "b"}
    assert n == 1


async def test_filter_live_rose_activation(monkeypatch, db):
    _enable(monkeypatch)
    # frozen threshold captured at enqueue; a member now exceeds it because
    # retrieved_count rose during the week.
    risen = _point("risen", confidence=0.5, retrieved_count=19, days_old=1)
    a, b = _point("a", confidence=0.4, days_old=120), _point("b", confidence=0.4, days_old=120)
    # threshold between the dull baseline and the risen member's live activation.
    survivors, n = await dream_shield.shield_filter_live(
        db, [risen, a, b], activation_threshold=0.30, centrality_threshold=None, now=NOW
    )
    assert "risen" not in {m["id"] for m in survivors}
    assert n == 1


async def test_filter_live_disabled_is_noop(monkeypatch, db):
    monkeypatch.setattr(dream_shield_config, "shield_enabled", lambda: False)
    cluster = [_point("a", confidence=0.99), _point("b")]
    survivors, n = await dream_shield.shield_filter_live(
        db, cluster, activation_threshold=0.0, centrality_threshold=None, now=NOW
    )
    assert survivors == cluster
    assert n == 0
