"""Tests for the immunity_status health MCP tool (WS-3 B1 obs read surface).

The tool wires the GROUNDWORK `immunity_shadow.recent_summary` read to a
discoverable health surface: per-gate mode + per-site would-block counts. The
core requirement is that it stays GATE-AGNOSTIC — when gates 1-3 land and write
their own rows, they must surface here without a code change.
"""

from __future__ import annotations

from genesis.mcp.health.immunity_status import _impl_immunity_status, _shape
from genesis.security import immunity, immunity_shadow

# ── pure shaping logic (gate-agnostic + counting) ──────────────────────────


def test_shape_reports_all_canonical_gates_even_with_zero_rows():
    out = _shape(
        summary_rows=[
            {
                "gate": "injection",
                "source_ref": "mcp/memory/core.py::memory_recall",
                "would_block": 1,
                "n": 3,
            },
        ],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    assert out["status"] == "ok"
    assert set(out["gates"]) == set(immunity.GATES)
    assert out["gates"]["injection"]["would_block_total"] == 3
    # A gate with no rows still appears, with an empty site list.
    assert out["gates"]["identity"]["would_block_total"] == 0
    assert out["gates"]["identity"]["by_site"] == []


def test_shape_is_gate_agnostic_surfaces_non_injection_rows():
    # The whole point: a procedure-gate row (from the concurrent gates-1-3 work)
    # surfaces here without any code change.
    out = _shape(
        summary_rows=[
            {"gate": "procedure", "source_ref": "learning/spine.py", "would_block": 1, "n": 2},
            {"gate": "injection", "source_ref": "mcp/memory/core.py", "would_block": 1, "n": 5},
        ],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    assert out["gates"]["procedure"]["would_block_total"] == 2
    assert out["gates"]["injection"]["would_block_total"] == 5


def test_shape_never_drops_an_unknown_gate():
    # A row for a gate not in the canonical set must NOT be silently dropped —
    # it surfaces with mode "unknown" so a schema/emit drift is visible.
    out = _shape(
        summary_rows=[
            {"gate": "future_gate", "source_ref": "x.py", "would_block": 1, "n": 4},
        ],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    assert "future_gate" in out["gates"]
    assert out["gates"]["future_gate"]["mode"] == "unknown"
    assert out["gates"]["future_gate"]["would_block_total"] == 4


def test_shape_aggregates_multiple_sites_sorted_desc():
    out = _shape(
        summary_rows=[
            {"gate": "injection", "source_ref": "site_a", "would_block": 1, "n": 3},
            {"gate": "injection", "source_ref": "site_b", "would_block": 1, "n": 5},
        ],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    inj = out["gates"]["injection"]
    assert inj["would_block_total"] == 8
    # Highest-volume site first.
    assert [s["source_ref"] for s in inj["by_site"]] == ["site_b", "site_a"]
    assert [s["count"] for s in inj["by_site"]] == [5, 3]


def test_shape_ignores_would_block_zero_rows():
    # would_block=0 rows (forward-compat: gates 1-3 may write them) must not be
    # counted as would-blocks.
    out = _shape(
        summary_rows=[
            {"gate": "injection", "source_ref": "site_a", "would_block": 0, "n": 9},
        ],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    assert out["gates"]["injection"]["would_block_total"] == 0
    assert out["gates"]["injection"]["by_site"] == []


def test_shape_reports_window():
    out = _shape(
        summary_rows=[],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since="2026-07-01T00:00:00+00:00",
    )
    assert out["window"] == "2026-07-01T00:00:00+00:00"
    out2 = _shape(
        summary_rows=[],
        gate_modes={g: "shadow" for g in immunity.GATES},
        master_enabled=True,
        since=None,
    )
    assert out2["window"] == "all-time"


# ── _impl wiring (mode join + read, no DB) ─────────────────────────────────


async def test_impl_joins_live_modes(monkeypatch):
    async def fake_summary(*, since=None, db=None):
        return [{"gate": "injection", "source_ref": "s", "would_block": 1, "n": 1}]

    monkeypatch.setattr(immunity_shadow, "recent_summary", fake_summary)
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    monkeypatch.setattr(immunity, "load_immunity_config", lambda: {"enabled": True})

    out = await _impl_immunity_status()
    assert out["status"] == "ok"
    assert out["master_enabled"] is True
    assert out["gates"]["injection"]["mode"] == "shadow"
    assert out["gates"]["injection"]["would_block_total"] == 1


async def test_impl_master_disabled_reports_off(monkeypatch):
    async def fake_summary(*, since=None, db=None):
        return []

    monkeypatch.setattr(immunity_shadow, "recent_summary", fake_summary)
    # Do NOT patch gate_mode — let it derive "off" from the disabled master.
    monkeypatch.setattr(immunity, "load_immunity_config", lambda: {"enabled": False})

    out = await _impl_immunity_status()
    assert out["master_enabled"] is False
    assert all(g["mode"] == "off" for g in out["gates"].values())


async def test_impl_returns_unavailable_on_read_error(monkeypatch):
    # The one operator-visible failure path: if the read raises past its own
    # best-effort guard, the tool must report "unavailable", not silently-wrong data.
    async def boom(*, since=None, db=None):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(immunity_shadow, "recent_summary", boom)
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    monkeypatch.setattr(immunity, "load_immunity_config", lambda: {"enabled": True})

    out = await _impl_immunity_status()
    assert out["status"] == "unavailable"


async def test_impl_forwards_since_window(monkeypatch):
    captured = {}

    async def fake_summary(*, since=None, db=None):
        captured["since"] = since
        return []

    monkeypatch.setattr(immunity_shadow, "recent_summary", fake_summary)
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    monkeypatch.setattr(immunity, "load_immunity_config", lambda: {"enabled": True})

    out = await _impl_immunity_status(since="2026-07-01T00:00:00+00:00")
    assert captured["since"] == "2026-07-01T00:00:00+00:00"
    assert out["window"] == "2026-07-01T00:00:00+00:00"
