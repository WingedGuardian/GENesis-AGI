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
) -> dict:
    """Pure comparator — everything the markdown renders, as data."""
    from genesis.session_awareness.ledger_extractor import best_match

    live_runs = [r for r in runs if include_backfill or r["trigger"] != "backfill"]
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
    ambient_leaks = [r for r in ledger_rows if r.get("added_by") == "ambient"]

    return {
        "n_runs": n_runs,
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
        "truncation_rate": truncation_rate,
        "pivots": pivots,
        "backfill_events": backfill_events,
        "leak_invariant_ok": len(ambient_leaks) == 0,
        "ambient_leaks": ambient_leaks,
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
        f"- **Leak invariant** (zero ambient rows in live ledger):"
        f" {'HELD' if report['leak_invariant_ok'] else 'VIOLATED — INVESTIGATE'}",
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
