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
    prev = _profile({"host_system": _section(None, status="unavailable")})
    curr = _profile({"host_system": _section("x", {"cores": 8})})
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
    bus = AsyncMock()
    bus.emit = lambda *a, **k: bus.emitted.append((a, k))
    bus.emitted = []
    with patch(
        "genesis.db.crud.observations.create",
        new=AsyncMock(side_effect=RuntimeError("db locked")),
    ):
        written = await emit_drift_observations(object(), drift, event_bus=bus)
    assert written == 0
    assert len(bus.emitted) == 1


async def test_emit_without_db_is_noop():
    assert await emit_drift_observations(None, [{"section": "x"}]) == 0
