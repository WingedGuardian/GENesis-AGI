"""Shadow precision report: comparator math (recomputed matching,
late-ratified TP, duplicate collapsing, FN windowing, leak invariant,
backfill exclusion) + markdown rendering."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "ledger_shadow_report", _SCRIPTS_DIR / "ledger_shadow_report.py"
)
_rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rep)

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


def _run(run_id: str, status: str = "ok", *, trigger: str = "manual", started: str, **over):
    row = dict(
        run_id=run_id,
        session_id=SID,
        started_at=started,
        finished_at=started,
        start_byte=0,
        end_byte=100,
        trigger=trigger,
        status=status,
        truncated=0,
        latency_ms=12000,
        mode="shadow",
    )
    row.update(over)
    return row


def _event(eid: str, text: str, *, run_id: str, kind: str = "agreement", **over):
    row = dict(
        id=eid,
        run_id=run_id,
        observed_at="2026-07-14T12:00:00+00:00",
        session_id=SID,
        kind=kind,
        text=text,
        turn_ref="u-1",
        quote_preview="yes, do that",
        quote_verified=1,
        match_kind="none",
        matched_item_id=None,
        duplicate_of=None,
        mode="shadow",
    )
    row.update(over)
    return row


def _fg_row(rid: str, text: str, created: str = "2026-07-14T11:00:00+00:00"):
    return {
        "id": rid,
        "session_id": SID,
        "text": text,
        "status": "open",
        "added_by": "foreground",
        "created_at": created,
    }


T1 = "2026-07-14T12:00:00+00:00"


def test_tp_fp_precision_recomputed():
    runs = [_run("r1", started=T1)]
    events = [
        _event("e1", "wire the rollback lever before the refactor ships", run_id="r1"),
        _event("e2", "buy milk and eggs on the way home", run_id="r1"),
    ]
    ledger = [_fg_row("L1", "wire the rollback lever before the refactor ships")]
    rep = _rep.build_report(runs, events, ledger)
    assert len(rep["tp"]) == 1 and len(rep["fp"]) == 1
    assert rep["precision"] == 0.5
    assert rep["tp"][0]["recomputed_item"] == "L1"


def test_late_ratified_tp_flagged():
    """Stored match_kind said none (row didn't exist at run time); the
    recomputation counts it TP and flags the late ratification."""
    runs = [_run("r1", started=T1)]
    events = [_event("e1", "wire the rollback lever", run_id="r1", match_kind="none")]
    ledger = [_fg_row("L1", "wire the rollback lever", created="2026-07-14T13:00:00+00:00")]
    rep = _rep.build_report(runs, events, ledger)
    assert len(rep["tp"]) == 1
    assert rep["tp"][0]["late_ratified"] is True


def test_duplicate_chain_collapses_to_one_proposal():
    runs = [_run("r1", started=T1), _run("r2", started="2026-07-14T13:00:00+00:00")]
    events = [
        _event("e1", "wire the rollback lever", run_id="r1"),
        _event("e2", "wire the rollback lever", run_id="r2", duplicate_of="e1"),
    ]
    rep = _rep.build_report(runs, events, [])
    assert rep["n_unique_agreements"] == 1
    assert rep["precision"] == 0.0  # one FP (no ledger rows)


def test_fn_windowing_charges_only_swept_rows():
    """A foreground row created AFTER the last successful run started is
    not yet swept — it must not count as FN. Failed runs sweep nothing."""
    runs = [
        _run("r1", started="2026-07-14T12:00:00+00:00"),
        _run("r2", status="failed", started="2026-07-14T14:00:00+00:00"),
    ]
    ledger = [
        _fg_row("L1", "swept and missed", created="2026-07-14T11:00:00+00:00"),
        _fg_row("L2", "not yet swept", created="2026-07-14T13:00:00+00:00"),
    ]
    rep = _rep.build_report(runs, [], ledger)
    assert [r["id"] for r in rep["fn"]] == ["L1"]


def test_fn_matched_by_proposal_not_charged():
    runs = [_run("r1", started=T1)]
    events = [_event("e1", "wire the rollback lever", run_id="r1")]
    ledger = [_fg_row("L1", "wire the rollback lever", created="2026-07-14T11:00:00+00:00")]
    rep = _rep.build_report(runs, events, ledger)
    assert rep["fn"] == []
    assert rep["recall"] == 1.0


def test_backfill_excluded_by_default_included_on_flag():
    runs = [
        _run("r1", started=T1),
        _run("rb", trigger="backfill", started="2026-07-13T00:00:00+00:00"),
    ]
    events = [
        _event("e1", "live proposal", run_id="r1"),
        _event("eb", "backfill proposal", run_id="rb"),
    ]
    rep = _rep.build_report(runs, events, [])
    assert rep["n_unique_agreements"] == 1
    assert len(rep["backfill_events"]) == 1
    rep2 = _rep.build_report(runs, events, [], include_backfill=True)
    assert rep2["n_unique_agreements"] == 2
    assert rep2["backfill_events"] == []


def test_leak_invariant_detects_ambient_rows():
    runs = [_run("r1", started=T1)]
    ambient_row = dict(_fg_row("LX", "sneaky ambient write"), added_by="ambient")
    rep = _rep.build_report(runs, [], [ambient_row])
    assert rep["leak_invariant_ok"] is False
    md = _rep.render_md(rep, generated_at=T1)
    assert "LEAK INVARIANT VIOLATION" in md


def test_health_metrics_and_render():
    runs = [
        _run("r1", started=T1, latency_ms=10000),
        _run("r2", status="timeout", started="2026-07-14T13:00:00+00:00", latency_ms=120000),
        _run("r3", status="empty_delta", started="2026-07-14T14:00:00+00:00", truncated=1),
    ]
    events = [_event("e1", "a proposal", run_id="r1", quote_verified=0)]
    rep = _rep.build_report(runs, events, [])
    assert rep["n_runs"] == 3
    assert rep["status_histogram"] == {"ok": 1, "timeout": 1, "empty_delta": 1}
    assert abs(rep["failure_rate"] - 1 / 3) < 1e-9
    assert rep["quote_verified_rate"] == 0.0
    assert abs(rep["truncation_rate"] - 1 / 3) < 1e-9
    md = _rep.render_md(rep, generated_at=T1)
    assert "Agreement precision" in md
    assert "- [ ] `aaaabbbb` **a proposal**" in md  # FP adjudication checkbox
    assert "HELD" in md


def test_pivots_listed_never_scored():
    runs = [_run("r1", started=T1)]
    events = [_event("p1", "pivoted to incident response", run_id="r1", kind="pivot")]
    rep = _rep.build_report(runs, events, [])
    assert rep["n_unique_agreements"] == 0
    assert rep["precision"] is None
    assert len(rep["pivots"]) == 1
    md = _rep.render_md(rep, generated_at=T1)
    assert "pivoted to incident response" in md
