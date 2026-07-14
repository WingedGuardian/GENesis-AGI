"""Drift detection: hash-change semantics + dedup-gated observation writes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from genesis.infra_profile.diff import compute_drift, emit_drift_observations


def _profile(sections):
    return {"sections": sections}


def _section(h, facts=None, status="ok"):
    return {"hash": h, "facts": facts or {}, "status": status}


def test_first_run_emits_nothing():
    assert compute_drift({}, _profile({"cpu": _section("a")})) == []


def test_unchanged_hash_no_drift():
    prev = _profile({"cpu": _section("a")})
    curr = _profile({"cpu": _section("a")})
    assert compute_drift(prev, curr) == []


def test_changed_hash_drifts_with_paths():
    prev = _profile({"kernel": _section("a", {"release": "6.8.0-133"})})
    curr = _profile({"kernel": _section("b", {"release": "6.8.0-134"})})
    drift = compute_drift(prev, curr)
    assert len(drift) == 1
    assert drift[0]["section"] == "kernel"
    assert drift[0]["priority"] == "high"  # kernel is a high-priority section
    assert "release" in drift[0]["changed_paths"][0]


def test_new_section_is_not_drift():
    prev = _profile({"cpu": _section("a")})
    curr = _profile({"cpu": _section("a"), "host_system": _section("x")})
    assert compute_drift(prev, curr) == []


def test_unavailable_transitions_are_not_drift():
    # A section that was never ok has no hash — first data ≠ drift.
    prev = _profile({"host_system": _section(None, status="unavailable")})
    curr = _profile({"host_system": _section("x", {"cores": 8})})
    assert compute_drift(prev, curr) == []


def test_fact_change_across_outage_window_is_drift():
    # service.py preserves last-ok facts+hash through an unavailable window;
    # a fact that REALLY changed while the plane was down must surface on
    # recovery (review 2026-07-13 — a status gate used to swallow this).
    prev = _profile(
        {"host_virt": _section("old", {"limits": "8"}, status="unavailable")},
    )
    curr = _profile({"host_virt": _section("new", {"limits": "5"})})
    drift = compute_drift(prev, curr)
    assert len(drift) == 1
    assert drift[0]["section"] == "host_virt"


def test_going_unavailable_keeps_hash_no_drift():
    # Curr unavailable keeps the prior hash (service merge) — no drift.
    prev = _profile({"host_virt": _section("same", {"limits": "8"})})
    curr = _profile(
        {"host_virt": _section("same", {"limits": "8"}, status="unavailable")},
    )
    assert compute_drift(prev, curr) == []


async def test_emit_writes_dedup_gated_observation():
    drift = [
        {
            "section": "storage",
            "old_hash": "a",
            "new_hash": "b",
            "changed_paths": ["mounts.0.options"],
            "truncated": False,
            "priority": "high",
        },
    ]
    with patch("genesis.db.crud.observations.create", new=AsyncMock(return_value="id1")) as create:
        written = await emit_drift_observations(object(), drift)
    assert written == 1
    kwargs = create.call_args.kwargs
    assert kwargs["source"] == "infra_profile"
    assert kwargs["type"] == "infrastructure_drift"
    assert kwargs["skip_if_duplicate"] is True
    assert kwargs["content_hash"]  # stable per (section, old, new)
    assert "mounts.0.options" in kwargs["content"]


async def test_emit_dedup_skip_counts_zero():
    drift = [
        {
            "section": "cpu",
            "old_hash": "a",
            "new_hash": "b",
            "changed_paths": [],
            "truncated": False,
            "priority": "medium",
        },
    ]
    with patch("genesis.db.crud.observations.create", new=AsyncMock(return_value=None)):
        written = await emit_drift_observations(object(), drift)
    assert written == 0


async def test_emit_db_failure_falls_back_to_event_bus():
    drift = [
        {
            "section": "cpu",
            "old_hash": "a",
            "new_hash": "b",
            "changed_paths": [],
            "truncated": False,
            "priority": "medium",
        },
    ]
    # emit is a coroutine on the real GenesisEventBus and is AWAITED by the
    # fallback — the fake must be async too (a sync lambda here masked an
    # unawaited-coroutine bug; Codex P2 2026-07-12).
    bus = AsyncMock()
    bus.emitted = []

    async def _emit(*a, **k):
        bus.emitted.append((a, k))

    bus.emit = _emit
    with patch(
        "genesis.db.crud.observations.create",
        new=AsyncMock(side_effect=RuntimeError("db locked")),
    ):
        written = await emit_drift_observations(object(), drift, event_bus=bus)
    assert written == 0
    assert len(bus.emitted) == 1


async def test_emit_without_db_is_noop():
    assert await emit_drift_observations(None, [{"section": "x"}]) == 0
