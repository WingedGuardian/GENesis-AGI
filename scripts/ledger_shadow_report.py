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
    several runs — precision must count it once. A dangling duplicate_of
    (root pruned) keeps the event as its own root.
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
        roots.setdefault(cur["id"], cur)
    return list(roots.values())


def build_report(
    runs: list[dict],
    events: list[dict],
    ledger_rows: list[dict],
    *,
    include_backfill: bool = False,
    mode: str | None = None,
) -> dict:
    """Pure comparator — everything the markdown renders, as data.

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
    backfill_events = [e for e in events if e["run_id"] not in live_run_ids]

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
    fn: list[dict] = []
    for sid, rows in fg_by_session.items():
        horizon = last_ok_by_session.get(sid)
        if horizon is None:
            continue
        proposals = [e for e in agreements if e["session_id"] == sid]
        for row in rows:
            if (row.get("created_at") or "") > horizon:
                continue  # not yet swept — charged to no run
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
    run_mode = {r["run_id"]: (r.get("mode") or "shadow") for r in runs}
    promoted_under_other = {
        e["promoted_item_id"]
        for e in events
        if e.get("promoted_item_id") and run_mode.get(e["run_id"]) != "live"
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
        for e in events
        if e.get("promoted_item_id") and run_mode.get(e["run_id"]) == "live"
    }
    # UNATTRIBUTED rows are leaks, and the most serious kind. Every legitimate
    # promotion stamps `promoted_item_id` on its shadow event, so a row wearing
    # the extractor's provenance that NO event claims was written by something
    # else using that identity — which is precisely what this invariant exists
    # to catch. Counting it separately keeps the three causes distinguishable
    # in the report; folding it into "not a leak" would have made the invariant
    # weaker than the version this change set replaced.
    unattributed = [
        r for r in extractor_rows
        if r["id"] not in promoted_under_live and r["id"] not in promoted_under_other
    ]
    leaks = [
        r for r in extractor_rows
        if r["id"] in promoted_under_other                       # written while NOT live
        or r in unattributed                                     # no event claims it
        # `source_quote` is the provenance field; `evidence` is a fallback for
        # rows promoted before the column existed. Asking `evidence` alone is
        # what made a repo-pulse absorption look like a leak — it replaces that
        # column with PR attribution, so the quote was simply gone.
        or (r["id"] in promoted_under_live
            and not (r.get("source_quote") or r.get("evidence")))
    ]
    leak_label = (
        "extractor rows are legitimate only if promoted by a run in live mode "
        "AND carrying their source quote"
    )

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
        "leak_invariant_ok": len(leaks) == 0,
        "leak_invariant_label": leak_label,
        "leak_mode": mode,
        "ambient_leaks": leaks,
        "n_extractor_rows": len(extractor_rows),
        "include_backfill": include_backfill,
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


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
        f"- Runs: {report['n_runs']} across {report['sessions_covered']} session(s);"
        f" status {report['status_histogram']};"
        f" failure rate {_fmt_rate(report['failure_rate'])};"
        f" latency p50/p90 {report['latency_p50_ms']}/{report['latency_p90_ms']} ms",
        f"- **Leak invariant** ({report['leak_invariant_label']}):"
        f" {'HELD' if report['leak_invariant_ok'] else 'VIOLATED — INVESTIGATE'}"
        f"  ·  extractor rows: {report['n_extractor_rows']}",
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
    if not report["leak_invariant_ok"]:
        lines += ["", "## LEAK INVARIANT VIOLATION", ""]
        for row in report["ambient_leaks"]:
            lines.append(f"- `{row['session_id'][:8]}` row {row['id']}: {row['text']}")
    lines.append("")
    return "\n".join(lines)


async def _load(db_path: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Read runs/events/ledger via the CRUD layer (RO connection).

    Failures are LOUD (stderr): a silently-empty table would make the
    leak-invariant check vacuously HELD and the metrics meaningless.
    """
    import aiosqlite

    from genesis.db.crud.session_charters import ledger_all
    from genesis.db.crud.session_ledger_shadow import list_events, list_runs

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = aiosqlite.Row

        async def _all(reader, label: str, **kwargs) -> list[dict]:
            try:
                return await reader(db, **kwargs)
            except Exception as exc:
                print(f"ledger_shadow_report: {label} read failed: {exc}", file=sys.stderr)
                return []

        runs = await _all(list_runs, "shadow runs", limit=1000)
        events = await _all(list_events, "shadow events", limit=2000)
        ledger = await _all(ledger_all, "session_ledger", limit=10000)
        # list_runs returns newest-first; the comparator wants oldest-first
        runs.sort(key=lambda r: r.get("started_at") or "")
        return runs, events, ledger


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="genesis.db path (default: repo data dir)")
    ap.add_argument("--out", default=None, help="output md path (default: ~/.genesis/output/)")
    ap.add_argument("--include-backfill", action="store_true")
    args = ap.parse_args()

    from genesis.env import genesis_db_path

    db_path = args.db or str(genesis_db_path())
    runs, events, ledger = asyncio.run(_load(db_path))
    report = build_report(runs, events, ledger, include_backfill=args.include_backfill)
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
        f"leak_invariant={'HELD' if report['leak_invariant_ok'] else 'VIOLATED'}"
    )
    print(f"report: {out}")


if __name__ == "__main__":
    main()
