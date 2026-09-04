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

from genesis.session_awareness.ledger_extractor import (  # noqa: E402
    PROMPT_VERSION,
)

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
        # The report now scopes its precision population to the CURRENT prompt
        # version — mixing versions makes the headline number describe a
        # population that no longer exists. A fixture without this field is a
        # legacy run by definition, so it would be excluded and every metric
        # would read zero. Tests that WANT the legacy case override it.
        prompt_version=PROMPT_VERSION,
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


def _extractor_row(rid, text, *, evidence=None, source_quote=None):
    """A ledger row shaped exactly as `_promote_live` writes one."""
    return dict(
        _fg_row(rid, text),
        added_by="ambient_ledger_extractor",
        evidence=evidence,
        source_quote=source_quote,
    )


def _promotion_event(eid, rid, *, run_id):
    """The shadow event that ATTRIBUTES a promoted ledger row to its run.

    Every real promotion writes one — `_promote_live` stamps
    `promoted_item_id` immediately after `ledger_add`. A test row without it
    describes a shape the system cannot produce, and the report now treats an
    unattributable extractor row as a leak precisely because something other
    than the promotion path must have written it.
    """
    return _event(eid, "promoted proposal", run_id=run_id, promoted_item_id=rid)


def test_leak_invariant_flags_an_extractor_row_in_shadow_mode():
    """In shadow/off the extractor must write NOTHING live. Any row is a leak.

    Keyed on the EXTRACTOR's own provenance value. It previously keyed on
    "ambient", which is what a DISPATCHED CC session stamps — a different
    writer entirely — so the first dispatched session to add a ledger row would
    have been reported as an extractor leak.
    """
    runs = [_run("r1", started=T1)]
    rep = _rep.build_report(
        runs, [], [_extractor_row("LX", "written in shadow")], mode="shadow"
    )
    assert rep["leak_invariant_ok"] is False
    assert "LEAK INVARIANT VIOLATION" in _rep.render_md(rep, generated_at=T1)


def test_a_dispatched_session_row_is_not_an_extractor_leak():
    """The false-positive side, and the reason the values must stay distinct.

    `_default_added_by()` stamps "ambient" on any dispatched CC session. That is
    a human-directed write, not the extractor, and reporting it as a leak would
    make the invariant cry wolf on the first one.
    """
    runs = [_run("r1", started=T1)]
    dispatched = dict(_fg_row("LY", "dispatched session wrote this"),
                      added_by="ambient")
    rep = _rep.build_report(runs, [], [dispatched], mode="shadow")
    assert rep["leak_invariant_ok"] is True


def test_live_mode_holds_when_every_extractor_row_carries_its_quote():
    """In live the rows are expected; the invariant becomes "show your source".

    This is the case that was VIOLATED by construction: the promotion filter
    demands a verified quote and `ledger_add` had no `evidence` column to put it
    in, so every promoted row failed and the report screamed on every normal
    run. An alarm that always fires is an alarm nobody reads.
    """
    runs = [_run("r1", started=T1, mode="live")]
    rep = _rep.build_report(
        runs,
        [_promotion_event("e1", "LZ", run_id="r1")],
        [_extractor_row("LZ", "promoted", source_quote="the source quote")],
        mode="live",
    )
    assert rep["leak_invariant_ok"] is True, rep["leaks"]


def test_a_rollback_does_not_retroactively_brand_rows_promoted_while_live():
    """The emergency rollback live -> shadow must not indict its own history.

    Rows promoted WHILE live are legitimate, and they persist across the
    rollback. Judging them by the mode active NOW turned every one of them into
    a leak the moment an operator rolled back — so the report screamed loudest
    about exactly the rows the rollback was protecting, which is how an operator
    learns to stop reading it.

    A row is judged by the mode of the run that promoted it, which each shadow
    event records.
    """
    runs = [_run("r_live", started=T1, mode="live"), _run("r_now", started="2026-07-15T12:00:00+00:00", mode="shadow")]
    rep = _rep.build_report(
        runs,
        [_promotion_event("e1", "LOLD", run_id="r_live")],
        [_extractor_row("LOLD", "promoted before the rollback",
                        source_quote="the source quote")],
        mode="shadow",          # rolled back since
    )
    assert rep["leak_invariant_ok"] is True, rep["leaks"]


def test_a_row_promoted_while_not_live_is_a_leak_even_after_going_live():
    """The other direction: going live does not launder a row written in shadow."""
    runs = [_run("r_shadow", started=T1, mode="shadow")]
    rep = _rep.build_report(
        runs,
        [_promotion_event("e1", "LBAD", run_id="r_shadow")],
        [_extractor_row("LBAD", "written while shadow", source_quote="q")],
        mode="live",
    )
    assert rep["leak_invariant_ok"] is False


def test_a_repo_pulse_absorption_does_not_look_like_a_leak():
    """`evidence` is a RESOLUTION field — repo-pulse replaces it with PR text.

    Sharing one column for provenance and resolution meant a promoted row lost
    its quote the moment repo-pulse absorbed it, and then failed the invariant
    it had satisfied the day before. `source_quote` is the provenance field and
    no resolver writes it.
    """
    runs = [_run("r1", started=T1, mode="live")]
    rep = _rep.build_report(
        runs,
        [_promotion_event("e1", "LABS", run_id="r1")],
        [_extractor_row("LABS", "absorbed by repo-pulse",
                        evidence="PR #1234: something (merged) [repo-pulse exact]",
                        source_quote="the original transcript quote")],
        mode="live",
    )
    assert rep["leak_invariant_ok"] is True, rep["leaks"]


def test_live_mode_flags_an_extractor_row_with_no_quote():
    """The other direction — the invariant must still be able to fire."""
    runs = [_run("r1", started=T1, mode="live")]
    rep = _rep.build_report(
        runs,
        [_promotion_event("e1", "LW", run_id="r1")],
        [_extractor_row("LW", "promoted, unsourced")], mode="live"
    )
    assert rep["leak_invariant_ok"] is False


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
