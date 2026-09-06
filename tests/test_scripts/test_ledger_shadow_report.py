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


# ── Round-8/9 review fixes: windows, buckets, floors, denominators ──────────


def _ext_row(rid, text, *, created="2026-07-14T12:30:00+00:00", quote="q"):
    return {
        "id": rid,
        "session_id": SID,
        "text": text,
        "status": "open",
        "added_by": "ambient_ledger_extractor",
        "created_at": created,
        "source_quote": quote,
        "evidence": quote,
    }


def test_attribution_is_read_by_key_never_through_the_window():
    """The leak verdict must see every promoted event, however old.

    Retention exempts promoted events so the verifier can always see them —
    but a verifier reading through a capped newest-first scan ages them out
    of its own LIMIT, and the invariant quietly narrows to recent-rows-only
    while saying HELD (or convicts an old legitimate row as unattributed).
    The attribution set is therefore passed SEPARATELY, queried by the key:
    here the windowed `events` list has lost the old claiming event, and the
    verdict is still correct because `attribution_events` carries it.
    The CONTROL shows the failure shape the separation prevents: without the
    attribution set, the same data convicts.
    """
    old_run = _run("r-old", started="2026-07-01T00:00:00+00:00", mode="live")
    old_claim = _event("e-old", "an old promoted agreement", run_id="r-old",
                       observed_at="2026-07-01T00:00:05+00:00",
                       promoted_item_id="LX")
    windowed_events = [_event("e1", "recent proposal", run_id="r1",
                              observed_at="2026-07-14T12:00:00+00:00")]
    runs = [_run("r1", started=T1, mode="live"), old_run]
    old_row = _ext_row("LX", "an old promoted agreement",
                       created="2026-07-01T00:00:06+00:00")

    rep = _rep.build_report(
        runs, windowed_events, [old_row], attribution_events=[old_claim]
    )
    assert rep["leak_invariant_ok"] is True, rep["ambient_leaks"]

    # CONTROL: the windowed fallback (no attribution set) convicts the same
    # row — which is exactly why the CLI loader always passes the real set.
    rep = _rep.build_report(runs, windowed_events, [old_row])
    assert rep["leak_invariant_ok"] is False


def test_an_attributed_row_with_a_missing_run_is_excused_and_labeled():
    """A claiming event whose run row is GONE is attributed, mode unknown.

    Producible on installs that pruned before the exemption shipped (the old
    prune deleted runs unconditionally). Convicting would permanently
    VIOLATED every such install; vouching 'live' would state more than the
    data supports. It is excused from conviction and counted under its
    honest cause — 'run row missing', not a query-window story — and an
    UNQUOTED row in this state still trips the quote clause, because the
    quote lives on the row and needs no run lookup."""
    claim = _event("e1", "promoted thing", run_id="r-gone",
                   promoted_item_id="LP")
    row = _ext_row("LP", "promoted thing")
    rep = _rep.build_report([], [], [row], attribution_events=[claim])
    assert rep["leak_invariant_ok"] is True, rep["ambient_leaks"]
    assert rep["n_attributed_run_missing"] == 1

    # An unquoted attributed row is a promotion-filter failure whichever
    # mode wrote it — the quote check does not need the run.
    bare = dict(row, source_quote=None, evidence=None)
    rep = _rep.build_report([], [], [bare], attribution_events=[claim])
    assert rep["leak_invariant_ok"] is False


def test_legacy_events_are_not_filed_as_backfill():
    """v1 compaction proposals must not render under 'Backfill proposals'.

    'run_id not in the current-version set' used to be the backfill test, so
    on any upgraded install the whole v1 corpus was mislabelled backfill.
    Classification now reads the event's own run: trigger for backfill,
    version for legacy, absence for window-orphan."""
    runs = [
        _run("r-v1", started=T1, prompt_version="v1"),
        _run("r-bf", started=T1, trigger="backfill"),
    ]
    events = [
        _event("e-v1", "a v1 proposal", run_id="r-v1"),
        _event("e-bf", "a backfill proposal", run_id="r-bf"),
        _event("e-orphan", "an orphan proposal", run_id="r-gone"),
    ]
    rep = _rep.build_report(runs, events, [])
    assert [e["id"] for e in rep["legacy_events"]] == ["e-v1"]
    assert [e["id"] for e in rep["backfill_events"]] == ["e-bf"]
    assert [e["id"] for e in rep["window_orphan_events"]] == ["e-orphan"]

    md = _rep.render_md(rep, generated_at=T1)
    assert "Legacy prompt-version proposals (excluded; 1)" in md
    assert "a v1 proposal" in md.split("Legacy prompt-version")[1]


def test_fn_floor_excludes_rows_swept_by_the_previous_version():
    """Rows the OLD prompt already covered are not the new prompt's misses.

    The cursor never rewinds on a version bump, so a foreground row created
    before v1's last successful sweep was never in v2's delta — charging it
    as a v2 FN depressed recall with structurally-impossible misses. A row
    created AFTER that floor stays chargeable (the fresh-install case, with
    no legacy runs at all, is the existing FN tests, which still pass)."""
    runs = [
        _run("r-v1", started="2026-07-14T10:00:00+00:00", prompt_version="v1"),
        _run("r-v2", started="2026-07-14T12:00:00+00:00"),
    ]
    ledger = [
        _fg_row("L-old", "an agreement v1 swept", created="2026-07-14T09:00:00+00:00"),
        _fg_row("L-new", "an agreement v2 missed", created="2026-07-14T11:00:00+00:00"),
    ]
    rep = _rep.build_report(runs, [], ledger)
    assert [r["id"] for r in rep["fn"]] == ["L-new"], (
        "the pre-v2 row was charged as a v2 miss"
    )


def test_headline_renders_the_version_denominator():
    """The narrowed population must SAY it is narrowed, where it is read.

    build_report already recorded version/excluded counts; neither the
    markdown nor the CLI consumed them, so a small fresh-v2 subset read as
    the full retained corpus at flip-adjudication time."""
    runs = [
        _run("r1", started=T1),
        _run("r-v1", started=T1, prompt_version="v1"),
    ]
    rep = _rep.build_report(runs, [], [])
    md = _rep.render_md(rep, generated_at=T1)
    assert f"prompt **{PROMPT_VERSION}** only" in md
    assert "1 other-version run(s) excluded" in md
    assert "v1" in md


def test_attribution_runs_stay_out_of_the_metric_populations():
    """Attribution runs exist for MODE JUDGMENT, not health statistics.

    Merged into `runs`, an event-less old run (loaded only because its
    promotion is retention-exempt) entered the status histogram and extended
    the FN horizon past the loaded events — so an old-only session accrued
    false negatives and the headline counted runs its event population never
    covered. Passed separately, the leak verdict still judges write-time mode
    while every metric population ignores them.
    """
    old_run = _run("r_old", started="2026-01-01T00:00:00+00:00", mode="live")
    promo_ev = _event(
        "e_old", "ship the thing", run_id="r_old", promoted_item_id="led-1",
    )
    led = _extractor_row("led-1", "ship the thing", source_quote="q")
    # A foreground row that predates nothing in the loaded window: with the
    # old run merged in, the FN horizon covered it and charged a miss.
    fg = _fg_row("fg-1", "totally unrelated commitment")

    rep = _rep.build_report(
        [],  # windowed runs: none loaded
        [],
        [led, fg],
        mode="live",
        attribution_events=[promo_ev],
        attribution_runs=[old_run],
    )
    assert rep["n_runs"] == 0, "an attribution-only run entered the headline"
    assert rep["status_histogram"] == {}
    assert rep["fn"] == [], (
        "an attribution-only run extended the FN horizon past the loaded events"
    )
    # ...while the verdict still judged the old promotion by its run's mode.
    assert rep["leak_invariant_ok"] is True
    assert rep["n_attributed_run_missing"] == 0


def test_dangling_duplicates_group_by_their_missing_root():
    """Descendants of one unloaded root are ONE agreement, not several.

    The worker's dedup pool points repeated proposals at the same oldest
    root; once that root falls outside the loaded run window, counting each
    descendant as its own root inflates the TP/FP denominator with copies.
    """
    runs = [_run("r1", started=T1)]
    events = [
        _event("e1", "ship the thing", run_id="r1", duplicate_of="e_gone"),
        _event("e2", "ship the thing", run_id="r1", duplicate_of="e_gone"),
        _event("e3", "a different thing", run_id="r1"),
    ]
    rep = _rep.build_report(runs, events, [])
    assert rep["n_unique_agreements"] == 2, (
        "two descendants of one missing root were counted separately"
    )


def test_incomplete_ledger_refuses_the_leak_verdict():
    """The invariant convicts on absence, so a partial ledger read cannot
    hold it — an empty list from a failed read used to render HELD, the
    exact vacuous all-clear the verdict exists to prevent."""
    runs = [_run("r1", started=T1, mode="live")]
    rep = _rep.build_report(runs, [], [], mode="live", ledger_complete=False)
    assert rep["leak_invariant_ok"] is None
    md = _rep.render_md(rep, generated_at=T1)
    assert "NOT ISSUED" in md
    assert "VIOLATED" not in md.split("NOT ISSUED")[0], (
        "a refused verdict must not render as a violation"
    )
    # And the complete default still yields a real verdict.
    held = _rep.build_report(runs, [], [], mode="live")
    assert held["leak_invariant_ok"] is True
