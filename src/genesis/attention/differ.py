"""A/B fire-set differ for the attention engine's shadow gate (offline calibration, PR3c-1).

Diffs two config versions' FIRE-SETS over the SAME ambient snapshot — which utterances each
config perked up on — so a taxonomy/weight change (e.g. PR3a's ``0.1.0-default`` →
``0.2.0-taxonomy``) can be evaluated OBJECTIVELY (events added / removed / activation-changed
+ a per-trigger delta) instead of by eyeballing ``by_trigger`` counts.

Each side is a list of ``FireRecord``s keyed by the TRIGGER utterance id (the window end).
A side comes from ONE of two sources:

  * **persisted rows** — ``attention_events`` for a ``config_version`` (how ``0.1.0-default``'s
    326 already-run rows are read; its config no longer exists in code — PR3a overwrote it);
  * **a live re-score** — ``run_shadow`` under the CURRENT config over the snapshot (how a
    fresh config's fire-set is produced without persisting anything; ``ShadowReport`` discards
    the full event list, so a ``_Collector`` captures it).

Both sources read the SAME snapshot, so the utterance-id spaces align and the sets compare 1:1.
"Fire-set" = every ``AttentionEvent`` emitted, i.e. HARD/SOFT perks AND SUPPRESSED vetoes
(``evaluate`` emits suppressed events too) — so a soft→suppressed shift shows as an
activation change, not a phantom add/remove.

Value-free: derives refs + trigger NAMES + activation only — NEVER transcript text (firewall).
A genesis-side ADAPTER (imports aiosqlite/runner) — NOT one of the six edge-portable core
modules and NOT imported by ``attention/__init__``.

    python -m genesis.attention.differ --baseline 0.1.0-default --rescore \\
        --snapshot ~/.genesis/attention/snapshots/ambient_20260701T013412Z.db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from genesis.attention.config import AttentionConfig
from genesis.attention.runner import load_runner_config, run_shadow
from genesis.attention.types import AttentionEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FireRecord:
    """One config's decision for a trigger utterance: activation + which names fired.

    ``key`` = the trigger-utterance id (window end) — the join key across the two sides,
    matching ``consumers._to_row``'s ``utt_ids[-1]`` row-id derivation."""

    key: int
    activation: str                 # "hard" / "soft" / "suppressed"
    triggers: frozenset[str]        # trigger NAMES only (no weights, no text)
    suppressors: frozenset[str]


@dataclass
class DiffResult:
    baseline_label: str
    candidate_label: str
    baseline_n: int
    candidate_n: int
    added: list[FireRecord]                            # fired in candidate, not baseline
    removed: list[FireRecord]                           # fired in baseline, not candidate
    activation_changed: list[tuple[int, str, str]]      # (key, baseline_act, candidate_act)
    by_trigger_delta: dict[str, tuple[int, int, int]]   # name -> (baseline_n, candidate_n, delta)
    by_suppressor_delta: dict[str, tuple[int, int, int]]  # name -> (baseline_n, candidate_n, delta)
    unresolvable: dict[str, int] = field(default_factory=dict)  # per side: rows skipped (empty utt_ids)


def diff_fire_sets(
    baseline: list[FireRecord],
    candidate: list[FireRecord],
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
    unresolvable: dict[str, int] | None = None,
) -> DiffResult:
    """PURE set/dict diff of two fire-sets keyed by trigger-utt id. No IO, no LLM, no labels.

    A key present on both sides with a different ``activation`` is an activation change (NOT
    an add+remove). ``by_trigger_delta`` counts each trigger once per record it appears in
    (mirroring the ``trigger_stats`` crud aggregate)."""
    b_by_key = {r.key: r for r in baseline}
    c_by_key = {r.key: r for r in candidate}
    b_keys, c_keys = set(b_by_key), set(c_by_key)

    added = [c_by_key[k] for k in sorted(c_keys - b_keys)]
    removed = [b_by_key[k] for k in sorted(b_keys - c_keys)]
    activation_changed = [
        (k, b_by_key[k].activation, c_by_key[k].activation)
        for k in sorted(b_keys & c_keys)
        if b_by_key[k].activation != c_by_key[k].activation
    ]

    by_trigger_delta = _count_delta(baseline, candidate, lambda r: r.triggers)
    by_suppressor_delta = _count_delta(baseline, candidate, lambda r: r.suppressors)

    return DiffResult(
        baseline_label, candidate_label, len(baseline), len(candidate),
        added, removed, activation_changed, by_trigger_delta, by_suppressor_delta,
        unresolvable or {},
    )


def _count_delta(baseline, candidate, extract) -> dict[str, tuple[int, int, int]]:
    """Per-name (baseline_n, candidate_n, delta), each name counted once per record it's in."""
    b = Counter(name for r in baseline for name in extract(r))
    c = Counter(name for r in candidate for name in extract(r))
    return {name: (b[name], c[name], c[name] - b[name]) for name in sorted(set(b) | set(c))}


# ── mappers (persisted row / live event → FireRecord) ─────────────────────────────

def _record_from_row(row: dict) -> FireRecord | None:
    """Map a persisted ``attention_events`` row (JSON columns still strings) → FireRecord.

    Returns None for a degenerate row whose ``window_ref`` has no ``utt_ids`` (the ``"x"``
    fallback in ``consumers._to_row``) — the caller counts these as ``unresolvable``."""
    wr = json.loads(row["window_ref"]) if row.get("window_ref") else {}
    utt_ids = wr.get("utt_ids") or []
    if not utt_ids:
        return None
    hits = json.loads(row["triggers_fired"]) if row.get("triggers_fired") else []
    triggers = frozenset(h["name"] for h in hits if h.get("name"))
    suppressors = frozenset(json.loads(row["suppressors"]) if row.get("suppressors") else [])
    return FireRecord(int(utt_ids[-1]), row["activation"], triggers, suppressors)


def _record_from_event(ev: AttentionEvent) -> FireRecord | None:
    """Map a live ``AttentionEvent`` (rescore side) → FireRecord (None if no window)."""
    utt_ids = ev.window_ref.utt_ids
    if not utt_ids:
        return None
    return FireRecord(
        int(utt_ids[-1]),
        ev.activation.value,
        frozenset(h.name for h in ev.triggers_fired),
        frozenset(ev.suppressors),
    )


# ── IO adapters (persisted DB read / live re-score) ───────────────────────────────

class _Collector:
    """In-memory ``ShadowConsumer`` — captures the FULL fire-set ``ShadowReport`` discards.

    Satisfies the ``ShadowConsumer`` Protocol (``add`` + async ``flush``). ``flush`` writes
    nothing (returns 0 → ``ShadowReport.persisted == 0``); the differ reads ``self.events``."""

    def __init__(self) -> None:
        self.events: list[AttentionEvent] = []

    def add(self, ev: AttentionEvent) -> None:
        self.events.append(ev)

    async def flush(self) -> int:
        return 0


async def load_from_db(db_path: str | Path, config_version: str) -> tuple[list[FireRecord], int]:
    """Fire-set for a PERSISTED config_version, read from a READ-ONLY genesis.db connection.

    Opens its own ``aiosqlite`` connection (``?mode=ro`` — the differ never writes) and sets
    ``row_factory = aiosqlite.Row`` (the crud reads assume it). Returns (records, n_unresolvable)."""
    from genesis.db.crud import attention as attention_crud

    uri = f"file:{Path(db_path).expanduser()}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True)
    try:
        conn.row_factory = aiosqlite.Row
        rows = await attention_crud.load_fire_records(conn, config_version)
    finally:
        await conn.close()
    return _map_records(_record_from_row, rows)


async def rescore(snapshot_path: str | Path, config: AttentionConfig) -> tuple[list[FireRecord], int]:
    """Fire-set from a LIVE re-score of the snapshot under ``config``. Returns (records, n_unresolvable).

    Reuses ``run_shadow`` (blip-skip + evaluate) via an in-memory ``_Collector``; persists nothing."""
    collector = _Collector()
    await run_shadow(snapshot_path, config, consumer=collector)
    return _map_records(_record_from_event, collector.events)


def _map_records(mapper, items) -> tuple[list[FireRecord], int]:
    """Apply ``mapper`` to each item; split into resolved records + a count of unresolvable ones."""
    records: list[FireRecord] = []
    unresolvable = 0
    for item in items:
        rec = mapper(item)
        if rec is None:
            unresolvable += 1
        else:
            records.append(rec)
    return records, unresolvable


# ── report ────────────────────────────────────────────────────────────────────────

def format_diff(d: DiffResult, *, sample: int = 12) -> str:
    """Human-readable diff (utt ids + trigger names + activations only — NEVER text)."""
    lines = [
        "\n=== ATTENTION A/B FIRE-SET DIFF ===",
        f"baseline  [{d.baseline_label}]  fires={d.baseline_n}",
        f"candidate [{d.candidate_label}]  fires={d.candidate_n}",
        f"  added     (candidate only): {len(d.added)}",
        f"  removed   (baseline only):  {len(d.removed)}",
        f"  activation-changed (both):  {len(d.activation_changed)}",
    ]
    if d.unresolvable and any(d.unresolvable.values()):
        lines.append(f"  unresolvable (empty utt_ids, skipped): {d.unresolvable}")
    lines.append("\n--- by_trigger delta (baseline -> candidate, largest movers first) ---")
    for name, (b, c, delta) in sorted(d.by_trigger_delta.items(), key=lambda x: (-abs(x[1][2]), x[0])):
        sign = "+" if delta > 0 else ""
        lines.append(f"  {name:<22} {b:>5} -> {c:<5} ({sign}{delta})")
    if d.by_suppressor_delta:
        lines.append("\n--- by_suppressor delta (baseline -> candidate) ---")
        for name, (b, c, delta) in sorted(d.by_suppressor_delta.items(), key=lambda x: (-abs(x[1][2]), x[0])):
            sign = "+" if delta > 0 else ""
            lines.append(f"  {name:<22} {b:>5} -> {c:<5} ({sign}{delta})")
    if d.activation_changed:
        lines.append(f"\n--- sample activation changes ({min(len(d.activation_changed), sample)} of {len(d.activation_changed)}) ---")
        for k, fr, to in d.activation_changed[:sample]:
            lines.append(f"  utt {k}: {fr} -> {to}")
    for label, recs in (("added", d.added), ("removed", d.removed)):
        if recs:
            lines.append(f"\n--- sample {label} ({min(len(recs), sample)} of {len(recs)}) ---")
            for r in recs[:sample]:
                lines.append(f"  utt {r.key}: [{r.activation}] {sorted(r.triggers)}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────────--

async def _run(args) -> DiffResult:
    from genesis.env import genesis_db_path

    db_path = args.db or str(genesis_db_path())
    baseline, b_unres = await load_from_db(db_path, args.baseline)
    if args.candidate:
        candidate, c_unres = await load_from_db(db_path, args.candidate)
        cand_label = args.candidate
    else:  # --rescore (the default when no --candidate)
        cfg = load_runner_config(args.config)
        candidate, c_unres = await rescore(args.snapshot, cfg)
        cand_label = f"rescore:{cfg.version}"
    return diff_fire_sets(
        baseline, candidate,
        baseline_label=args.baseline, candidate_label=cand_label,
        unresolvable={args.baseline: b_unres, cand_label: c_unres},
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Attention A/B fire-set differ (offline calibration)")
    ap.add_argument("--baseline", required=True,
                    help="PERSISTED config_version for side A (e.g. 0.1.0-default)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--candidate", help="PERSISTED config_version for side B")
    g.add_argument("--rescore", action="store_true",
                   help="side B = live re-score of --snapshot under the current config (default)")
    ap.add_argument("--snapshot", help="snapshot .db for --rescore")
    ap.add_argument("--config", help="attention_config.json for --rescore (default: overlay/built-in)")
    ap.add_argument("--db", help="genesis.db path (default: genesis_db_path())")
    args = ap.parse_args()
    if not args.candidate and not args.snapshot:
        ap.error("--rescore (default) requires --snapshot PATH")
    print(format_diff(asyncio.run(_run(args))))


if __name__ == "__main__":
    main()
