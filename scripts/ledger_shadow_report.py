#!/usr/bin/env python3
"""Shadow precision report for the ambient ledger extractor (PR-3).

Compares shadow proposals (session_ledger_shadow_events) against the ground
truth — foreground `session_ledger_add` rows — and writes the adjudication
report the shadow-phase flip decision is made from:

- TP: a unique agreement proposal matching (exact/fuzzy >= 0.85) any
  foreground ledger row of its session. Matching is RECOMPUTED here against
  the CURRENT ledger — a row the user ratified AFTER the run (late-ratified
  TP) counts as TP even though the stored at-run-time match_kind said none.
- FP: no match — listed VERBATIM with an adjudication checkbox ("would I
  have wanted this row?"); human-marked would-wants reclassify at review.
- FN: foreground rows inside the swept window (created before the session's
  last successful non-backfill run started) matched by no proposal.
  Informational — the extractor is a safety net; low recall doesn't block
  the flip, low precision does.
- Health: run status histogram, latency, quote-verified + truncation rates,
  and the LEAK INVARIANT (ambient-authored rows in the live ledger must be
  ZERO during shadow).

Backfill runs (trigger='backfill') are EXCLUDED from precision metrics by
default (no ground truth existed for historical sessions) and listed
separately for eyeball review; --include-backfill folds them in.

Read-only (mode=ro URI). Output: markdown to ~/.genesis/output/ (or --out).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _collapse_duplicates(events: list[dict]) -> list[dict]:
    """Unique proposals: fold duplicate_of chains onto their root event.

    Crash-recovery re-covers windows, so the same agreement can appear in
    several runs — precision must count it once. A DANGLING duplicate_of
    (the root fell outside the loaded run window, or was pruned) still
    groups: every descendant of one missing root shares its target id, and
    the deduper points repeated proposals at the same oldest root — so
    counting each descendant as its own root inflated the TP/FP denominator
    with copies of one agreement. The first-seen descendant represents the
    group; its text is the matcher's input either way.
    """
    by_id = {e["id"]: e for e in events}
    roots: dict[str, dict] = {}
    for ev in events:
        cur = ev
        seen = {cur["id"]}
        while cur.get("duplicate_of") and cur["duplicate_of"] in by_id:
            nxt = by_id[cur["duplicate_of"]]
            if nxt["id"] in seen:  # defensive: cycle
                break
            seen.add(nxt["id"])
            cur = nxt
        dangling = cur.get("duplicate_of") and cur["duplicate_of"] not in by_id
        roots.setdefault(cur["duplicate_of"] if dangling else cur["id"], cur)
    return list(roots.values())


def build_report(
    runs: list[dict],
    events: list[dict],
    ledger_rows: list[dict],
    *,
    include_backfill: bool = False,
    mode: str | None = None,
    attribution_events: list[dict] | None = None,
    attribution_runs: list[dict] | None = None,
    ledger_complete: bool = True,
) -> dict:
    """Pure comparator — everything the markdown renders, as data.

    *attribution_events* is the COMPLETE set of promoted events (queried by
    the attribution key — ``list_promoted_events_with_runs``), which the leak
    verdict reads instead of the windowed *events* list: a verdict computed
    over "the newest N events" silently narrows to recent-rows-only as the
    retention-exempt attribution corpus outgrows the cap. ``None`` falls back
    to the windowed list — acceptable only for small/complete corpora (tests,
    fresh installs); the CLI loader always passes the real set.

    *attribution_runs* are those events' run rows, passed SEPARATELY from
    *runs* on purpose: they exist so the leak verdict can judge write-time
    mode however old the promotion is, and folding them into *runs* put
    event-less old runs inside the health statistics — an old-only session
    extended the FN horizon past its loaded events and accrued false
    negatives, and the headline described more runs than its event
    population covers. They feed mode judgment and event classification
    only, never the metric populations.

    *ledger_complete* is the loader's word that *ledger_rows* is the WHOLE
    table. The leak invariant convicts on absence, so over a partial or
    failed ledger read the verdict is NOT ISSUED (``leak_invariant_ok`` is
    ``None``) rather than vacuously HELD.

    *mode* selects how the leak invariant is judged; ``None`` reads the live
    config. It is a parameter because a hardcoded read is unredirectable, which
    makes the invariant's two branches untestable — and an invariant nobody can
    test is the one that turns out to be VIOLATED by construction.
    """
    from genesis.session_awareness.ledger_extractor import best_match

    live_runs = [r for r in runs if include_backfill or r["trigger"] != "backfill"]

    # PRECISION IS PER PROMPT VERSION. Runs record the version that produced
    # them, and mixing versions makes the headline number describe a population
    # that no longer exists: on an upgraded install, up to the full retention
    # window of v1 events would be scored as if the current prompt had emitted
    # them, so a v2 regression could hide behind v1's history — or v1's
    # mistakes could indict a v2 that never made them.
    #
    # Runs predating the column (version None) are counted as legacy, not as
    # current: an unknown version is not evidence about this one.
    from genesis.session_awareness.ledger_extractor import PROMPT_VERSION

    versions = {(r.get("prompt_version") or "unknown") for r in live_runs}
    legacy_runs = [r for r in live_runs if (r.get("prompt_version") or None) != PROMPT_VERSION]
    live_runs = [r for r in live_runs if (r.get("prompt_version") or None) == PROMPT_VERSION]
    live_run_ids = {r["run_id"] for r in live_runs}
    live_events = [e for e in events if e["run_id"] in live_run_ids]

    # Events OUTSIDE the current-version precision population are classified by
    # WHAT THEY ARE, never by which set they failed to join. "run_id not in
    # live_run_ids" used to file every one of them as backfill — so on any
    # upgraded install, v1's ordinary compaction proposals rendered under
    # "Backfill proposals", misdescribing the corpus the flip decision reads.
    # Three real categories, judged from the event's own run:
    #   - its run says trigger='backfill'    -> genuinely backfill
    #   - its run has another prompt version -> legacy corpus
    #   - its run was not loaded at all      -> outside the query window; a
    #     fact about OUR read (caps, retention), not about the event.
    # Attribution runs join the LOOKUP (mode judgment, event classification)
    # but never the populations above — live_runs/legacy_runs were filtered
    # from *runs* alone, which is what keeps event-less old runs out of the
    # histogram, the latency percentiles, and the FN horizon.
    all_runs_by_id = {r["run_id"]: r for r in [*runs, *(attribution_runs or [])]}
    backfill_events = []
    legacy_events = []
    window_orphan_events = []
    for e in events:
        if e["run_id"] in live_run_ids:
            continue
        run = all_runs_by_id.get(e["run_id"])
        if run is None:
            window_orphan_events.append(e)
        elif run["trigger"] == "backfill":
            backfill_events.append(e)
        else:
            legacy_events.append(e)

    foreground = [r for r in ledger_rows if r.get("added_by") == "foreground"]
    fg_by_session: dict[str, list[dict]] = {}
    for row in foreground:
        fg_by_session.setdefault(row["session_id"], []).append(row)

    unique = _collapse_duplicates(live_events)
    agreements = [e for e in unique if e["kind"] == "agreement"]
    pivots = [e for e in unique if e["kind"] == "pivot"]

    tp: list[dict] = []
    fp: list[dict] = []
    for ev in agreements:
        rows = fg_by_session.get(ev["session_id"], [])
        kind, matched_id, score = best_match(ev["text"], [(r["id"], r["text"]) for r in rows])
        entry = dict(ev, recomputed_match=kind, recomputed_item=matched_id, recomputed_score=score)
        if kind != "none":
            entry["late_ratified"] = ev.get("match_kind") == "none"
            tp.append(entry)
        else:
            fp.append(entry)

    # FN: foreground rows inside the swept window, matched by no proposal.
    # Swept window ≈ created before the session's LAST successful live run
    # started (the cursor had consumed everything written before that).
    ok_runs = [r for r in live_runs if r["status"] in ("ok", "empty_delta")]
    last_ok_by_session: dict[str, str] = {}
    for r in ok_runs:
        cur = last_ok_by_session.get(r["session_id"])
        if cur is None or r["started_at"] > cur:
            last_ok_by_session[r["session_id"]] = r["started_at"]
    # The floor of the FN window is the PREVIOUS prompt version's coverage.
    # The worker cursor never rewinds on a version bump, so bytes the old
    # prompt already consumed are bytes this version never saw — a foreground
    # row created before the legacy prompt's last successful sweep cannot have
    # a current-version proposal, and charging it here depressed recall with
    # misses this version was structurally unable to make. On a fresh install
    # there are no legacy runs, the floor is empty, and nothing changes. Time
    # is a proxy for the byte cursor, stated rather than hidden: rows created
    # in the gap between the legacy prompt's last sweep and the bump ARE
    # covered by this version's first run and stay chargeable.
    legacy_last_ok_by_session: dict[str, str] = {}
    for r in legacy_runs:
        if r["status"] in ("ok", "empty_delta"):
            cur = legacy_last_ok_by_session.get(r["session_id"])
            if cur is None or r["started_at"] > cur:
                legacy_last_ok_by_session[r["session_id"]] = r["started_at"]
    fn: list[dict] = []
    for sid, rows in fg_by_session.items():
        horizon = last_ok_by_session.get(sid)
        if horizon is None:
            continue
        floor = legacy_last_ok_by_session.get(sid) or ""
        proposals = [e for e in agreements if e["session_id"] == sid]
        for row in rows:
            created = row.get("created_at") or ""
            if created > horizon:
                continue  # not yet swept — charged to no run
            if created <= floor:
                continue  # swept (if at all) by a previous prompt version
            kind, _, _ = best_match(row["text"], [(e["id"], e["text"]) for e in proposals])
            if kind == "none":
                fn.append(row)

    n_unique = len(agreements)
    precision = (len(tp) / n_unique) if n_unique else None
    recall_denom = len(tp) + len(fn)
    recall = (len(tp) / recall_denom) if recall_denom else None

    status_hist: dict[str, int] = {}
    for r in live_runs:
        status_hist[r["status"]] = status_hist.get(r["status"], 0) + 1
    n_runs = len(live_runs)
    n_bad = status_hist.get("failed", 0) + status_hist.get("timeout", 0)
    latencies = sorted(r["latency_ms"] for r in live_runs if r.get("latency_ms"))

    def _pct(p: float) -> int | None:
        return latencies[int(len(latencies) * p)] if latencies else None

    quote_verified_rate = (
        sum(1 for e in agreements if e.get("quote_verified")) / n_unique if n_unique else None
    )
    truncation_rate = sum(1 for r in live_runs if r.get("truncated")) / n_runs if n_runs else None
    # The invariant is about the EXTRACTOR's own rows, and it has to key on the
    # extractor's own provenance value. It previously keyed on "ambient", which
    # is what `_default_added_by()` stamps on any DISPATCHED CC session — a
    # different writer entirely. That read clean only because no dispatched
    # session had yet added a ledger row; the first one would have been reported
    # as an extractor leak.
    #
    # MODE-CONDITIONAL, because "wrote nothing live" stops being the invariant
    # the moment live mode is the point:
    #   off/shadow — ANY extractor row is a leak. The worker must not write.
    #   live       — extractor rows are expected, but each must carry a VERIFIED
    #                quote. An unquoted row means the promotion filter let
    #                through something the extractor could not source in the
    #                transcript, which is the failure worth catching once
    #                writing is allowed.
    extractor_rows = [
        r for r in ledger_rows if r.get("added_by") == "ambient_ledger_extractor"
    ]
    run_mode = {
        r["run_id"]: (r.get("mode") or "shadow")
        for r in [*runs, *(attribution_runs or [])]
    }
    # ATTRIBUTION IS NEVER READ THROUGH THE WINDOWED SCAN. The precision
    # populations above are windowed by design; the leak invariant is not a
    # population metric — it is a verdict, and a verdict computed over "the
    # newest 2000 events" quietly narrows to "recent rows only" as the
    # retention-exempt attribution corpus ages past the report's own cap
    # (kept forever so the verifier can see it, aging out of the verifier's
    # own LIMIT). The loader hands the COMPLETE attribution set separately
    # (`attribution_events`, queried by the attribution key), and every
    # promoted_under_* judgment below reads THAT. A claiming event whose run
    # row is genuinely absent — a pre-exemption prune deleted it, or an
    # anomaly, since run+events commit together — is attributed-but-mode-
    # unknown: excused from conviction (convicting would permanently VIOLATE
    # every install that pruned before the exemption shipped) and labeled by
    # its actual cause, not by a query-window story the complete read
    # disproves.
    attribution = attribution_events if attribution_events is not None else events
    attributed_run_missing = {
        e["promoted_item_id"]
        for e in attribution
        if e.get("promoted_item_id") and e["run_id"] not in all_runs_by_id
    }
    promoted_under_other = {
        e["promoted_item_id"]
        for e in attribution
        if e.get("promoted_item_id")
        and e["run_id"] in all_runs_by_id
        and run_mode.get(e["run_id"]) != "live"
    }
    if mode is None:
        from genesis.session_awareness.ledger_shadow_config import effective_mode

        mode = effective_mode()
    # A row is judged by the mode active WHEN IT WAS WRITTEN, not by the mode
    # now. The documented emergency rollback is live -> shadow, and those rows
    # persist — so keying on the current mode made every legitimately-promoted
    # row retroactively a leak the moment an operator rolled back. The report
    # would then scream about the very rows the rollback was protecting, which
    # is how an operator learns to ignore it.
    #
    # Each event records the row it promoted, and its run records the mode it
    # ran under. That is the provenance; the current mode is not.
    promoted_under_live = {
        e["promoted_item_id"]
        for e in attribution
        if e.get("promoted_item_id")
        and e["run_id"] in all_runs_by_id
        and run_mode.get(e["run_id"]) == "live"
    }
    # UNATTRIBUTED rows are leaks, and the most serious kind. Every legitimate
    # promotion stamps `promoted_item_id` on its shadow event, and the
    # attribution set above is COMPLETE (queried by the key, never windowed) —
    # so a row wearing the extractor's provenance that no event claims was
    # written by something else using that identity, which is precisely what
    # this invariant exists to catch. Counting it separately keeps the causes
    # distinguishable in the report; folding it into "not a leak" would have
    # made the invariant weaker than the version this change set replaced.
    unattributed = [
        r for r in extractor_rows
        if r["id"] not in promoted_under_live
        and r["id"] not in promoted_under_other
        and r["id"] not in attributed_run_missing
    ]
    unattributed_ids = {r["id"] for r in unattributed}
    leaks = [
        r for r in extractor_rows
        if r["id"] in promoted_under_other                       # written while NOT live
        or r["id"] in unattributed_ids                           # no event claims it
        # `source_quote` is the provenance field; `evidence` is a fallback for
        # rows promoted before the column existed. Asking `evidence` alone is
        # what made a repo-pulse absorption look like a leak — it replaces that
        # column with PR attribution, so the quote was simply gone.
        #
        # The quote requirement also covers attributed rows whose RUN row is
        # missing: mode may be unknowable from a dangling reference, but the
        # quote lives on the row itself and needs no run lookup — an unquoted
        # promoted row is a promotion-filter failure whichever mode wrote it.
        or (
            (r["id"] in promoted_under_live or r["id"] in attributed_run_missing)
            and not (r.get("source_quote") or r.get("evidence"))
        )
    ]
    leak_label = (
        "extractor rows are legitimate only if promoted by a run in live mode "
        "AND carrying their source quote"
    )
    # The invariant convicts on ABSENCE (a row no event claims), so it can
    # only be judged over the complete ledger. A failed or partial ledger
    # read yields an empty/short extractor_rows list, which reads as HELD —
    # the exact vacuous all-clear this verdict exists to prevent. Tri-state:
    # True held, False violated, None not issued.
    leak_ok: bool | None = (len(leaks) == 0) if ledger_complete else None
    if not ledger_complete:
        leak_label += " — VERDICT NOT ISSUED: ledger read failed or incomplete"

    return {
        "n_runs": n_runs,
        # The population this report DESCRIBES, stated rather than implied. A
        # narrowed denominator that is not reported reads as a complete one,
        # and precision computed over a silently-filtered set is the number
        # most likely to be quoted without its caveat.
        "prompt_version": PROMPT_VERSION,
        "prompt_versions_present": sorted(versions),
        "n_runs_excluded_other_version": len(legacy_runs),
        "status_histogram": status_hist,
        "failure_rate": (n_bad / n_runs) if n_runs else None,
        "latency_p50_ms": _pct(0.5),
        "latency_p90_ms": _pct(0.9),
        "sessions_covered": len({r["session_id"] for r in live_runs}),
        "n_events_total": len(live_events),
        "n_unique_agreements": n_unique,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "quote_verified_rate": quote_verified_rate,
        # Extractor rows that NO shadow event claims to have promoted. Not
        # leaks by the write-time-mode rule — nothing says which mode wrote
        # them — but not clean either, and lumping them into either bucket
        # would state more than the data supports.
        "n_extractor_rows_unattributed": len(unattributed),
        "truncation_rate": truncation_rate,
        "pivots": pivots,
        "backfill_events": backfill_events,
        "legacy_events": legacy_events,
        "window_orphan_events": window_orphan_events,
        "n_attributed_run_missing": len(attributed_run_missing),
        "leak_invariant_ok": leak_ok,
        "leak_invariant_label": leak_label,
        "leak_mode": mode,
        "ambient_leaks": leaks,
        "n_extractor_rows": len(extractor_rows),
        "include_backfill": include_backfill,
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _leak_verdict(ok: bool | None) -> str:
    """Tri-state: a verdict the data cannot support is stated as such."""
    if ok is None:
        return "NOT ISSUED — ledger read failed or incomplete"
    return "HELD" if ok else "VIOLATED — INVESTIGATE"


def render_md(report: dict, *, generated_at: str) -> str:
    """Render the build_report dict as the adjudication markdown."""
    lines = [
        "# Ledger Shadow Precision Report",
        "",
        f"Generated: {generated_at}  ·  backfill included in metrics: {report['include_backfill']}",
        "",
        "## Headline",
        "",
        f"- **Agreement precision (recomputed): {_fmt_rate(report['precision'])}**"
        f" ({len(report['tp'])} TP / {len(report['fp'])} FP over"
        f" {report['n_unique_agreements']} unique proposals)",
        f"- Recall (informational): {_fmt_rate(report['recall'])} ({len(report['fn'])} FN)",
        f"- Quote-verified: {_fmt_rate(report['quote_verified_rate'])}"
        f"  ·  truncated runs: {_fmt_rate(report['truncation_rate'])}",
        # The denominator is part of the number. A headline computed over a
        # version-narrowed population that does not SAY so reads as the full
        # retained corpus — which is exactly how a small fresh-v2 subset gets
        # mistaken for 45 days of evidence at flip-adjudication time.
        f"- Runs: {report['n_runs']} across {report['sessions_covered']} session(s)"
        f" — prompt **{report['prompt_version']}** only"
        f" ({report['n_runs_excluded_other_version']} other-version run(s) excluded;"
        f" versions present: {', '.join(report['prompt_versions_present']) or 'none'});"
        f" status {report['status_histogram']};"
        f" failure rate {_fmt_rate(report['failure_rate'])};"
        f" latency p50/p90 {report['latency_p50_ms']}/{report['latency_p90_ms']} ms",
        f"- **Leak invariant** ({report['leak_invariant_label']}):"
        f" {_leak_verdict(report['leak_invariant_ok'])}"
        f"  ·  extractor rows: {report['n_extractor_rows']}"
        + (
            f"  ·  attributed, run row missing "
            f"(pre-exemption prune or anomaly): "
            f"{report['n_attributed_run_missing']}"
            if report.get("n_attributed_run_missing")
            else ""
        ),
        "",
        "## False positives — adjudicate each (would you have wanted this row?)",
        "",
    ]
    if not report["fp"]:
        lines.append("(none)")
    for ev in report["fp"]:
        lines += [
            f"- [ ] `{ev['session_id'][:8]}` **{ev['text']}**",
            f"      quote: {ev.get('quote_preview') or '(none)'}"
            f"  ·  verified: {bool(ev.get('quote_verified'))}"
            f"  ·  turn: {ev.get('turn_ref') or '?'}",
        ]
    lines += ["", "## True positives", ""]
    if not report["tp"]:
        lines.append("(none)")
    for ev in report["tp"]:
        late = "  ·  LATE-RATIFIED" if ev.get("late_ratified") else ""
        lines.append(
            f"- `{ev['session_id'][:8]}` {ev['text']}"
            f"  ·  {ev['recomputed_match']} → {ev['recomputed_item']}{late}"
        )
    lines += ["", "## False negatives (informational — safety-net recall)", ""]
    if not report["fn"]:
        lines.append("(none)")
    for row in report["fn"]:
        lines.append(f"- `{row['session_id'][:8]}` {row['text']} (row {row['id'][:8]})")
    lines += ["", "## Pivots (no ground truth — eyeball review)", ""]
    if not report["pivots"]:
        lines.append("(none)")
    for ev in report["pivots"]:
        lines.append(f"- `{ev['session_id'][:8]}` {ev['text']}")
    if report["backfill_events"] and not report["include_backfill"]:
        lines += [
            "",
            f"## Backfill proposals (excluded from metrics; {len(report['backfill_events'])})",
            "",
        ]
        for ev in report["backfill_events"][:100]:
            lines.append(f"- `{ev['session_id'][:8]}` [{ev['kind']}] {ev['text']}")
    # Distinct from backfill on purpose: these are ordinary compaction
    # proposals from an earlier prompt version, and filing them under
    # "Backfill" misdescribed the corpus an adjudicator is reading.
    if report.get("legacy_events"):
        lines += [
            "",
            f"## Legacy prompt-version proposals (excluded; {len(report['legacy_events'])})",
            "",
        ]
        for ev in report["legacy_events"][:100]:
            lines.append(f"- `{ev['session_id'][:8]}` [{ev['kind']}] {ev['text']}")
    if report.get("window_orphan_events"):
        lines += [
            "",
            f"## Proposals whose run was not loaded (query window or missing; "
            f"excluded; {len(report['window_orphan_events'])})",
            "",
        ]
        # A bucket you can count but not read is half a disclosure — this
        # report exists for eyeball adjudication, so list them like the
        # sibling sections do.
        for ev in report["window_orphan_events"][:100]:
            lines.append(f"- `{ev['session_id'][:8]}` [{ev['kind']}] {ev['text']}")
    if report["leak_invariant_ok"] is False:
        lines += ["", "## LEAK INVARIANT VIOLATION", ""]
        for row in report["ambient_leaks"]:
            lines.append(f"- `{row['session_id'][:8]}` row {row['id']}: {row['text']}")
    lines.append("")
    return "\n".join(lines)


async def _load(
    db_path: str,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], bool]:
    """Read runs/events/ledger via the CRUD layer (RO connection).

    Failures are LOUD (stderr): a silently-empty table would make the
    leak-invariant check vacuously HELD and the metrics meaningless — and the
    ledger read carries an explicit completeness flag for exactly that
    reason, so build_report refuses the verdict instead of holding it
    vacuously.
    """
    import aiosqlite

    from genesis.db.crud.session_charters import ledger_all
    from genesis.db.crud.session_ledger_shadow import list_runs

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = aiosqlite.Row

        async def _all(reader, label: str, **kwargs) -> list[dict]:
            try:
                return await reader(db, **kwargs)
            except Exception as exc:
                print(f"ledger_shadow_report: {label} read failed: {exc}", file=sys.stderr)
                return []

        runs = await _all(list_runs, "shadow runs", limit=1000)
        # ONE JOINED WINDOW: events are selected BY the runs the report chose,
        # not by an independent row-count cap. Two independent newest-N caps
        # stop covering the same span the moment runs average more proposals
        # than the caps' ratio — runs whose events fell off then read as
        # swept-with-no-proposals and their foreground rows were charged as
        # false negatives while the headline still counted every run.
        events: list[dict] = []
        try:
            from genesis.db.crud.session_ledger_shadow import list_events_for_runs

            events = await list_events_for_runs(db, [r["run_id"] for r in runs])
        except Exception as exc:
            print(
                f"ledger_shadow_report: joined event read failed: {exc}",
                file=sys.stderr,
            )
        # ledger_all is COMPLETE (keyset-paginated; raises past its tripwire
        # rather than truncating). The flag says whether the read actually
        # delivered the whole table — the leak verdict convicts on absence,
        # so it is issued only over a complete corpus.
        ledger: list[dict] = []
        ledger_complete = False
        try:
            ledger = await ledger_all(db)
            ledger_complete = True
        except Exception as exc:
            print(
                f"ledger_shadow_report: session_ledger read failed: {exc}",
                file=sys.stderr,
            )
        # The ATTRIBUTION set is loaded by its key, never through the windows
        # above: the leak verdict must see every promoted event however old.
        # Its run rows travel SEPARATELY — build_report uses them for mode
        # judgment only; merging them into `runs` put event-less old runs
        # inside the health statistics and the FN horizon.
        attribution: list[dict] = []
        attr_runs: list[dict] = []
        try:
            from genesis.db.crud.session_ledger_shadow import (
                list_promoted_events_with_runs,
            )

            attribution, attr_runs = await list_promoted_events_with_runs(db)
        except Exception as exc:
            # LOUD, and fail toward the windowed fallback (which convicts
            # MORE, never less) rather than a silent all-clear.
            print(
                f"ledger_shadow_report: attribution read failed: {exc}",
                file=sys.stderr,
            )
            attribution = []
            attr_runs = []
        # the comparator wants both oldest-first
        runs.sort(key=lambda r: r.get("started_at") or "")
        events.sort(key=lambda e: e.get("observed_at") or "")
        return runs, events, ledger, attribution, attr_runs, ledger_complete


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="genesis.db path (default: repo data dir)")
    ap.add_argument("--out", default=None, help="output md path (default: ~/.genesis/output/)")
    ap.add_argument("--include-backfill", action="store_true")
    args = ap.parse_args()

    from genesis.env import genesis_db_path

    db_path = args.db or str(genesis_db_path())
    runs, events, ledger, attribution, attr_runs, ledger_complete = asyncio.run(
        _load(db_path)
    )
    report = build_report(
        runs,
        events,
        ledger,
        include_backfill=args.include_backfill,
        attribution_events=attribution or None,
        attribution_runs=attr_runs or None,
        ledger_complete=ledger_complete,
    )
    now = datetime.now(UTC)
    md = render_md(report, generated_at=now.isoformat())
    out = Path(
        args.out or Path.home() / ".genesis" / "output" / f"ledger-shadow-report-{now.date()}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(
        f"precision={_fmt_rate(report['precision'])} "
        f"recall={_fmt_rate(report['recall'])} "
        f"unique={report['n_unique_agreements']} runs={report['n_runs']} "
        f"prompt={report['prompt_version']} "
        f"excluded_other_version={report['n_runs_excluded_other_version']} "
        f"leak_invariant="
        f"{ {True: 'HELD', False: 'VIOLATED', None: 'NOT_ISSUED'}[report['leak_invariant_ok']] }"
    )
    print(f"report: {out}")


if __name__ == "__main__":
    main()
