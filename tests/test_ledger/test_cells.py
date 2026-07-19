"""WS-2 P3 cell-aggregation tests: hand-computed Murphy/shrinkage math, lane
partition (stated never contaminated by priors), window keying on resolved_at,
excluded statuses, the strict tool-call lane, empty-state no-op, history
append + retention prune, and upsert-then-prune rebuild semantics.

Graded rows are seeded via raw INSERTs (the aggregator reads terminal rows;
the CRUD's falsifiability gate rejects past deadlines, which every graded row
has by definition). One frozen clock (``NOW``) drives all window math — zero
wall-clock dependence. All Murphy fixtures place a single confidence value per
decile bin so the decomposition identity is EXACT (see cells.py docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.calibration.metrics import compute_ece
from genesis.db.crud import calibration_cells as cc_crud
from genesis.ledger.cells import (
    CellReport,
    compute_cell_stats,
    parent_domain,
    recompute_calibration_cells,
    shrink,
    status_for,
)

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


_SEQ = iter(range(10_000))


async def _graded(
    db,
    *,
    domain: str,
    confidence: float,
    outcome: int,
    provenance: str = "stated",
    resolved_days_ago: int = 10,
    resolver: str = "mechanical",
    status: str = "resolved",
    action_class: str = "outreach_send",
    metric: str = "reply_received",
):
    """Seed one terminal ledger row (or a non-terminal one for exclusion tests)."""
    outcome_value = outcome if status in ("resolved", "fuzzy_resolved") else None
    await db.execute(
        "INSERT INTO ledger_predictions (id, action_class, subject_ref_type,"
        " subject_ref_id, domain, metric, confidence, deadline_at, provenance,"
        " predictor, status, outcome_value, resolved_at, resolver)"
        " VALUES (?, ?, 'outreach', ?, ?, ?, ?, ?, ?, 'test', ?, ?, ?, ?)",
        (
            f"p-{next(_SEQ)}",
            action_class,
            f"s-{next(_SEQ)}",
            domain,
            metric,
            confidence,
            _ago(resolved_days_ago + 1),
            provenance,
            status,
            outcome_value,
            _ago(resolved_days_ago) if status not in ("open",) else None,
            resolver if outcome_value is not None else None,
        ),
    )
    await db.commit()


async def _tool_row(db, *, tool: str, success: int, days_ago: int = 10):
    await db.execute(
        "INSERT INTO tool_call_outcomes (session_id, tool_name, success, timestamp)"
        " VALUES ('s', ?, ?, ?)",
        (tool, success, _ago(days_ago)),
    )
    await db.commit()


async def _cells(db, **filters):
    return await cc_crud.list_cells(db, **filters)


def _one(cells, *, domain, provenance, window):
    matches = [
        c
        for c in cells
        if c["domain"] == domain and c["provenance"] == provenance and c["window_days"] == window
    ]
    assert len(matches) == 1, f"expected 1 cell, got {matches}"
    return matches[0]


# ── pure math ────────────────────────────────────────────────────────────────


def test_parent_domain():
    assert parent_domain("outreach.general") == "outreach"
    assert parent_domain("outreach") is None
    assert parent_domain("a.b.c") == "a"


@pytest.mark.parametrize(
    ("n", "expected"),
    [(9, "unknown"), (10, "thin"), (29, "thin"), (30, "ok"), (0, "unknown")],
)
def test_status_for_boundaries(n, expected):
    assert status_for(n) == expected


def test_shrink_hand_computed():
    # parent pool s=10, n=20 shrunk toward global 0.4 → (10 + 10·0.4)/30
    prior = shrink(10, 20, 0.4)
    assert prior == pytest.approx(14 / 30)
    # cell s=4, n=5 shrunk toward that prior → (4 + 10·(14/30))/15
    assert shrink(4, 5, prior) == pytest.approx((4 + 10 * (14 / 30)) / 15)


def test_compute_cell_stats_hand_computed():
    """Single confidence per decile bin → the Murphy identity is EXACT."""
    pairs = [(0.8, 1), (0.8, 1), (0.8, 1), (0.8, 0), (0.6, 1), (0.6, 0)]
    stats = compute_cell_stats(pairs)
    assert stats["n"] == 6
    assert stats["base_rate"] == pytest.approx(4 / 6)
    assert stats["mean_confidence"] == pytest.approx((0.8 * 4 + 0.6 * 2) / 6)
    # brier = (3·0.04 + 0.64 + 0.16 + 0.36) / 6
    assert stats["brier"] == pytest.approx(0.2133333, abs=1e-6)
    # rel = (4·(0.8−0.75)² + 2·(0.6−0.5)²) / 6
    assert stats["reliability"] == pytest.approx(0.005)
    # res = (4·(0.75−⅔)² + 2·(0.5−⅔)²) / 6
    assert stats["resolution"] == pytest.approx(0.0138889, abs=1e-6)
    assert stats["uncertainty"] == pytest.approx(2 / 9)
    # exact identity for this fixture
    assert stats["brier"] == pytest.approx(
        stats["reliability"] - stats["resolution"] + stats["uncertainty"]
    )
    # ECE agrees with a direct compute_ece over the same bins
    assert stats["ece"] == compute_ece(
        [
            {"sample_count": 4, "predicted_confidence": 0.8, "actual_success_rate": 0.75},
            {"sample_count": 2, "predicted_confidence": 0.6, "actual_success_rate": 0.5},
        ]
    )


def test_compute_cell_stats_empty():
    stats = compute_cell_stats([])
    assert stats["n"] == 0
    assert all(
        stats[k] is None
        for k in (
            "base_rate",
            "mean_confidence",
            "brier",
            "reliability",
            "resolution",
            "uncertainty",
            "ece",
        )
    )


# ── lanes, windows, exclusions ───────────────────────────────────────────────


async def test_lanes_partitioned_stated_never_sees_priors(db):
    for _ in range(2):
        await _graded(db, domain="outreach.general", confidence=0.8, outcome=1, provenance="stated")
    for _ in range(3):
        await _graded(
            db, domain="outreach.general", confidence=0.5, outcome=0, provenance="policy_prior"
        )
    await recompute_calibration_cells(db, now=NOW)
    cells = await _cells(db, window_days=0)
    stated = _one(cells, domain="outreach.general", provenance="stated", window=0)
    prior = _one(cells, domain="outreach.general", provenance="policy_prior", window=0)
    both = _one(cells, domain="outreach.general", provenance="all", window=0)
    assert stated["n"] == 2
    assert stated["base_rate"] == pytest.approx(1.0)  # priors did not leak in
    assert stated["mean_confidence"] == pytest.approx(0.8)
    assert prior["n"] == 3
    assert both["n"] == 5


async def test_windows_keyed_on_resolved_at(db):
    await _graded(db, domain="outreach.a", confidence=0.5, outcome=1, resolved_days_ago=10)
    await _graded(db, domain="outreach.a", confidence=0.5, outcome=1, resolved_days_ago=60)
    await _graded(db, domain="outreach.a", confidence=0.5, outcome=1, resolved_days_ago=200)
    await recompute_calibration_cells(db, now=NOW)
    cells = await _cells(db, provenance="stated")
    assert _one(cells, domain="outreach.a", provenance="stated", window=30)["n"] == 1
    assert _one(cells, domain="outreach.a", provenance="stated", window=90)["n"] == 2
    assert _one(cells, domain="outreach.a", provenance="stated", window=0)["n"] == 3


async def test_non_terminal_and_ungraded_rows_excluded(db):
    await _graded(db, domain="outreach.x", confidence=0.5, outcome=1)  # counts
    for status in ("open", "void", "unresolvable", "fuzzy_pending"):
        await _graded(db, domain="outreach.x", confidence=0.5, outcome=1, status=status)
    await recompute_calibration_cells(db, now=NOW)
    cells = await _cells(db)
    assert _one(cells, domain="outreach.x", provenance="stated", window=0)["n"] == 1


async def test_n_mechanical_counts_only_mechanical_resolvers(db):
    await _graded(
        db,
        domain="task.t",
        action_class="task_execution",
        metric="completed",
        confidence=0.5,
        outcome=1,
        resolver="mechanical",
    )
    await _graded(
        db,
        domain="task.t",
        action_class="task_execution",
        metric="completed",
        confidence=0.5,
        outcome=0,
        resolver="mechanical_absence",
    )
    await _graded(
        db,
        domain="task.t",
        action_class="task_execution",
        metric="completed",
        confidence=0.5,
        outcome=1,
        resolver="llm_fallback",
        status="fuzzy_resolved",
    )
    await recompute_calibration_cells(db, now=NOW)
    cell = _one(await _cells(db), domain="task.t", provenance="stated", window=0)
    assert cell["n"] == 3
    assert cell["n_mechanical"] == 2


async def test_ego_domains_do_get_cells_exclusion_is_reader_side(db):
    await _graded(
        db,
        domain="ego.notify",
        action_class="ego_proposal",
        metric="approved_and_executes",
        confidence=0.7,
        outcome=1,
    )
    await recompute_calibration_cells(db, now=NOW)
    assert _one(await _cells(db), domain="ego.notify", provenance="stated", window=0)["n"] == 1
    # ...and the reader-side switch drops them (incl. a hypothetical bare 'ego')
    assert await _cells(db, exclude_ego=True) == [
        c for c in await _cells(db) if not c["domain"].startswith("ego")
    ]


# ── shrinkage integration ────────────────────────────────────────────────────


async def test_shrinkage_toward_parent_toward_global(db):
    """Hand-built stratum: global g=0.4, parent pool (10/20) → cell 4/5."""
    for i in range(5):  # the cell: outreach.general, 4/5
        await _graded(db, domain="outreach.general", confidence=0.5, outcome=1 if i < 4 else 0)
    for i in range(15):  # sibling: outreach.other, 6/15 → parent pool 10/20
        await _graded(db, domain="outreach.other", confidence=0.5, outcome=1 if i < 6 else 0)
    for i in range(10):  # dotless 'misc': 2/10 → global pool 12/30 = 0.4
        await _graded(db, domain="misc", confidence=0.5, outcome=1 if i < 2 else 0)
    await recompute_calibration_cells(db, now=NOW)
    cells = await _cells(db, provenance="stated", window_days=0)
    cell = _one(cells, domain="outreach.general", provenance="stated", window=0)
    prior = (10 + 10 * 0.4) / 30  # parent shrunk toward global
    assert cell["base_rate"] == pytest.approx(0.8)
    assert cell["shrunk_estimate"] == pytest.approx((4 + 10 * prior) / 15)
    # dotless domain shrinks straight toward global
    misc = _one(cells, domain="misc", provenance="stated", window=0)
    assert misc["shrunk_estimate"] == pytest.approx((2 + 10 * 0.4) / 20)


# ── tool lane (strict §4.4) ──────────────────────────────────────────────────


async def test_tool_lane_strict_base_rate_only(db):
    for i in range(4):
        await _tool_row(db, tool="Edit", success=1 if i < 3 else 0, days_ago=10)
    await _tool_row(db, tool="Edit", success=0, days_ago=200)  # all-time only
    await recompute_calibration_cells(db, now=NOW)
    cells = await _cells(db, domain="tool")
    recent = _one(cells, domain="tool.Edit", provenance="policy_prior", window=90)
    alltime = _one(cells, domain="tool.Edit", provenance="policy_prior", window=0)
    assert recent["n"] == 4
    assert recent["base_rate"] == pytest.approx(0.75)
    assert recent["n_mechanical"] == 4
    assert recent["status"] == "unknown"  # n < 10
    assert alltime["n"] == 5
    assert alltime["base_rate"] == pytest.approx(0.6)
    for key in (
        "mean_confidence",
        "brier",
        "reliability",
        "resolution",
        "uncertainty",
        "ece",
        "shrunk_estimate",
    ):
        assert recent[key] is None, f"{key} must be NULL on tool cells"


# ── orchestration: empty state, history, rebuild ─────────────────────────────


async def test_empty_state_is_a_noop(db):
    report = await recompute_calibration_cells(db, now=NOW)
    assert report == CellReport(cells_written=0, history_appended=0, history_pruned=0)
    assert await _cells(db) == []


async def test_history_appends_per_pass_and_prunes_stale(db):
    await _graded(db, domain="outreach.h", confidence=0.5, outcome=1)
    # a stale snapshot beyond the 180d retention floor
    await db.execute(
        "INSERT INTO calibration_cell_history (id, domain, action_class, metric,"
        " provenance, window_days, n, status, snapshot_at)"
        " VALUES ('stale', 'outreach.h', 'outreach_send', 'reply_received',"
        " 'stated', 0, 1, 'unknown', ?)",
        (_ago(200),),
    )
    await db.commit()
    r1 = await recompute_calibration_cells(db, now=NOW)
    assert r1.history_pruned == 1  # the stale row
    r2 = await recompute_calibration_cells(db, now=NOW + timedelta(hours=12))
    assert r2.history_appended == r1.history_appended
    history = await cc_crud.list_history(db, domain="outreach.h")
    snapshots = {h["snapshot_at"] for h in history}
    assert len(snapshots) == 2  # one per pass, stale row gone


async def test_rebuild_replaces_stale_cells_without_duplicates(db):
    await _graded(db, domain="outreach.old", confidence=0.5, outcome=1)
    await recompute_calibration_cells(db, now=NOW)
    assert _one(await _cells(db), domain="outreach.old", provenance="stated", window=0)
    # source vanishes (e.g. rows re-voided by a data repair) → cell must go too
    await db.execute("DELETE FROM ledger_predictions")
    await db.commit()
    await _graded(db, domain="outreach.new", confidence=0.5, outcome=1)
    await recompute_calibration_cells(db, now=NOW + timedelta(hours=12))
    cells = await _cells(db)
    domains = {c["domain"] for c in cells}
    assert "outreach.old" not in domains
    assert "outreach.new" in domains
    # PK uniqueness: no duplicate cells after two passes
    keys = [
        (c["domain"], c["action_class"], c["metric"], c["provenance"], c["window_days"])
        for c in cells
    ]
    assert len(keys) == len(set(keys))


async def test_crud_rejects_invalid_enums(db):
    with pytest.raises(ValueError, match="provenance"):
        await cc_crud.replace_cells(
            db,
            [
                {
                    "domain": "d",
                    "action_class": "a",
                    "metric": "m",
                    "provenance": "vibes",
                    "window_days": 0,
                    "n": 1,
                    "n_mechanical": 1,
                    "status": "ok",
                }
            ],
            now=NOW,
        )
    with pytest.raises(ValueError, match="status"):
        await cc_crud.replace_cells(
            db,
            [
                {
                    "domain": "d",
                    "action_class": "a",
                    "metric": "m",
                    "provenance": "stated",
                    "window_days": 0,
                    "n": 1,
                    "n_mechanical": 1,
                    "status": "bogus",
                }
            ],
            now=NOW,
        )


# ── health surfacing ─────────────────────────────────────────────────────────


async def test_compute_alerts_surfaces_recompute_failure_counter():
    """_compute_alerts emits ledger:cell_recompute_failed while the counter is
    nonzero — stale derived data must not be silent (provision-or-surface)."""
    from genesis.ledger import cells as cells_mod
    from genesis.mcp.health import errors as health_errors

    cells_mod._recompute_failed["recompute"] = 2
    alerts, current_ids = await health_errors._compute_alerts()
    assert "ledger:cell_recompute_failed:recompute" in current_ids
    (alert,) = [a for a in alerts if a["id"] == "ledger:cell_recompute_failed:recompute"]
    assert alert["severity"] == "WARNING"
    assert "calibration_cells is stale" in alert["message"]

    cells_mod._reset_cell_counters_for_tests()
    alerts, current_ids = await health_errors._compute_alerts()
    assert not any(i.startswith("ledger:cell_recompute_failed") for i in current_ids)
