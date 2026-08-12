#!/usr/bin/env python3
"""MW-2 Checkpoint A — relationship-classifier quality measurement probe.

Samples real stored ``memory_links`` edges (auto_link / connection_pass's
similarity-derived >=0.75 population), runs the COARSE relationship classifier
via the REAL ``slm`` provider, and reports:

  1. **Classifier quality** — a hand-scorable sample dump (content_a, content_b,
     verdict, confidence) so a human can eyeball accuracy on real pairs. This is
     the DISPROVEN-IF gate: coarse accuracy <~70% → stop / escalate model tier.
  2. **Unsafe-edge fraction** — the verdict distribution over the stored
     similarity graph: what share is genuinely unsafe (contradicts / succeeded_by
     / duplicate) vs benign (distinct). This is the evidence that decides whether
     MW-2b (stored-graph reclassification + boost-gating) is worth building at all.

READ-ONLY: snapshots the live DB to ~/tmp via the sqlite online-backup API and
runs entirely against the copy. Nothing in production is mutated. The router's
cost tracker writes to the copy.

Usage:
  python scripts/dev/mw2_classifier_probe.py [--n 300] [--batch 15] [--seed 7]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

LIVE_DB = Path.home() / "genesis" / "data" / "genesis.db"
SNAP = Path.home() / "tmp" / "mw2_probe_snapshot.db"
OUT_DIR = Path.home() / ".genesis" / "output"

# auto_link writes extends/supports; connection_pass writes related_to. These are
# the similarity-derived (>=0.75) population MW-2 is about. strength >= 0.75
# excludes extraction-emitted typed links (exactly 0.7); synthesis provenance
# edges (extends@1.0) are excluded by SOURCE (dream_cycle_run_id LIKE
# 'synthesis:%' — the dream_link_repair marker) rather than by strength, so
# exact-cosine auto_link edges (prime duplicate suspects) STAY in (Codex r2).
_SIMILARITY_TYPES = ("extends", "supports", "related_to")

#: Size of the seeded REPRESENTATIVE hand-scoring subset (proportional across
#: verdicts by construction — stable-key order over all classified pairs).
_HAND_SCORE_N = 40


def snapshot_live_db() -> None:
    """WAL-safe online backup of the live DB → ~/tmp copy (read source, never write it)."""
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    if SNAP.exists():
        SNAP.unlink()
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(SNAP))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


async def run(n: int, batch: int, seed: int) -> int:
    snapshot_live_db()

    import aiosqlite

    from genesis.db.crud import memory as memory_crud
    from genesis.memory.relationship_classifier import (
        COARSE_RELATIONSHIPS,
        classify_relationships,
    )
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.config import load_config
    from genesis.routing.cost_tracker import CostTracker
    from genesis.routing.degradation import DegradationTracker
    from genesis.routing.litellm_delegate import LiteLLMDelegate
    from genesis.routing.router import Router

    db = await aiosqlite.connect(str(SNAP))
    # The runtime sets this; the router's cost-tracker (budgets.list_active does
    # dict(row)) requires it, and hydrate_for_expansion's index access still works.
    db.row_factory = aiosqlite.Row
    try:
        # FULL eligible population (Codex r2: a pre-LIMIT on a lexicographic
        # ORDER sampled only the earliest id-prefix cluster and no seed could
        # ever reach later edges). Similarity population = the 3 types at
        # strength >= 0.75, minus TRUE synthesis-provenance edges only:
        # source has the synthesis marker AND strength is exactly 1.0 (the
        # provenance-edge constant). A live dream-merge also REWIRES originals'
        # similarity edges onto the synthesis preserving their cosine (<1.0) —
        # those remain boost-live and stay in the population (Codex r3).
        rows = await db.execute_fetchall(
            "SELECT source_id, target_id, link_type, strength FROM memory_links "  # noqa: S608 - placeholders bound
            f"WHERE link_type IN ({','.join('?' * len(_SIMILARITY_TYPES))}) "
            "AND strength >= 0.75 "
            "AND NOT (link_type = 'extends' AND strength = 1.0 AND source_id IN ("
            "    SELECT memory_id FROM memory_metadata "
            "    WHERE dream_cycle_run_id LIKE 'synthesis:%'))",
            _SIMILARITY_TYPES,
        )
        eligible_edges = len(rows)

        # Dedup to UNORDERED pairs (A->B and B->A, or multi-type rows, are the
        # same content pair — classifying both would double-count it); keep the
        # strongest row per pair.
        by_pair: dict[tuple[str, str], tuple] = {}
        for r in rows:
            key = (min(r[0], r[1]), max(r[0], r[1]))
            if key not in by_pair or r[3] > by_pair[key][3]:
                by_pair[key] = tuple(r)
        rows = list(by_pair.values())
        eligible_pairs = len(rows)

        # Stable pseudo-shuffle by seed over the FULL population, then take n.
        # md5, not builtin hash(): PEP-456 salts str hashing per process, which
        # would make the same --seed draw a DIFFERENT sample every run.
        def _stable_key(r) -> int:
            digest = hashlib.md5(  # not crypto — a stable shuffle key
                f"{seed}:{r[0]}:{r[1]}".encode(), usedforsecurity=False
            ).hexdigest()
            return int(digest[:8], 16)

        rows.sort(key=_stable_key)
        rows = rows[:n]
        if not rows:
            print("No similarity edges found — nothing to probe.", file=sys.stderr)
            return 2

        ids = sorted({r[0] for r in rows} | {r[1] for r in rows})
        # Chunk to stay under SQLite's 999-bind ceiling at large --n (Codex P2);
        # hydrate_for_expansion itself builds ONE IN-clause.
        hydrated: dict[str, dict] = {}
        for off in range(0, len(ids), 400):
            hydrated.update(await memory_crud.hydrate_for_expansion(db, ids[off : off + 400]))

        # created_at per endpoint → per-pair chronology hint (Codex r2: the
        # batch API accepts newers; the probe must actually supply them).
        created_at: dict[str, str] = {}
        for off in range(0, len(ids), 400):
            chunk = ids[off : off + 400]
            ph = ",".join("?" * len(chunk))
            for mid, ca_ts in await db.execute_fetchall(
                f"SELECT memory_id, created_at FROM memory_metadata "  # noqa: S608 - placeholders bound
                f"WHERE memory_id IN ({ph})",
                chunk,
            ):
                if ca_ts:
                    created_at[mid] = ca_ts

        now_iso = datetime.now(UTC).isoformat()
        pairs: list[tuple[str, str]] = []
        newers: list[str | None] = []
        meta: list[dict] = []
        skipped_invisible = 0
        for src_id, tgt_id, ltype, strength in rows:
            row_a = hydrated.get(src_id) or {}
            row_b = hydrated.get(tgt_id) or {}
            ca = row_a.get("content")
            cb = row_b.get("content")
            if not (ca and cb):
                continue
            # Visibility parity with the boost path (graph_expansion skips
            # bitemporally-expired / deprecated rows): an edge whose endpoint
            # recall would hide cannot affect boosting — measuring it would
            # inflate the boost-risk population (Codex r2).
            visible = True
            for row in (row_a, row_b):
                inv = row.get("invalid_at")
                if (inv is not None and inv <= now_iso) or row.get("deprecated"):
                    visible = False
            if not visible:
                skipped_invisible += 1
                continue
            ts_a, ts_b = created_at.get(src_id), created_at.get(tgt_id)
            newer = ("a" if ts_a > ts_b else "b") if ts_a and ts_b and ts_a != ts_b else None
            pairs.append((ca, cb))
            newers.append(newer)
            meta.append(
                {
                    "source_id": src_id,
                    "target_id": tgt_id,
                    "stored_link_type": ltype,
                    "strength": strength,
                }
            )

        if not pairs:
            print("Edges sampled but no content hydrated — aborting.", file=sys.stderr)
            return 2

        # Real router (wing_backfill pattern) — keys from inherited env.
        config_path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
        config = load_config(config_path)
        router = Router(
            config=config,
            breakers=CircuitBreakerRegistry(config.providers),
            cost_tracker=CostTracker(db=db),
            degradation=DegradationTracker(),
            delegate=LiteLLMDelegate(config),
        )

        verdicts: list[dict] = []
        for i in range(0, len(pairs), batch):
            chunk = pairs[i : i + batch]
            verdicts.extend(
                await classify_relationships(router, chunk, newers=newers[i : i + batch])
            )
            print(
                f"  classified {min(i + batch, len(pairs))}/{len(pairs)} pairs...", file=sys.stderr
            )

        # ── tally ──────────────────────────────────────────────────────────
        # Fail-safes (LLM/parse failure, out-of-vocab) are NOT verdicts — counting
        # them as "distinct" would silently deflate the unsafe fraction (Codex P1).
        classified = [v for v in verdicts if not v.get("failed")]
        failsafe_count = len(verdicts) - len(classified)
        dist = Counter(v["relationship"] for v in classified)
        total = len(classified)
        if not total:
            print("Every classification failed — no distribution to report.", file=sys.stderr)
            return 2
        unsafe = dist["contradicts"] + dist["succeeded_by"] + dist["duplicate"]
        conf_by_rel: dict[str, list[float]] = {r: [] for r in COARSE_RELATIONSHIPS}
        for v in classified:
            conf_by_rel[v["relationship"]].append(v["confidence"])
        strengths = [m["strength"] for m in meta]

        def _mean(xs):
            return round(sum(xs) / len(xs), 3) if xs else 0.0

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "snapshot": str(SNAP),
            "eligible_edges": eligible_edges,
            "eligible_pairs_deduped": eligible_pairs,
            "sampled_pairs": len(rows),
            "skipped_invisible_endpoints": skipped_invisible,
            "classified_pairs": total,
            "failsafe_unclassified": failsafe_count,
            "stored_type_mix": dict(Counter(m["stored_link_type"] for m in meta)),
            "strength_spread": {
                "min": round(min(strengths), 3),
                "max": round(max(strengths), 3),
                "mean": _mean(strengths),
            },
            "verdict_distribution": {r: dist[r] for r in sorted(dist)},
            "verdict_pct": {r: round(100 * dist[r] / total, 1) for r in sorted(dist)},
            "unsafe_fraction_pct": round(100 * unsafe / total, 1),
            "mean_confidence_by_verdict": {r: _mean(conf_by_rel[r]) for r in COARSE_RELATIONSHIPS},
        }

        # ── hand-scoring artifacts (Codex r3) ─────────────────────────────
        # (a) hand_score_sample: a SEEDED REPRESENTATIVE subset across ALL
        #     verdicts (stable-key order, so distinct — 73% of the population —
        #     is proportionally present and overall accuracy / unsafe
        #     false-negatives are measurable from the artifact);
        # (b) unsafe_dump: every predicted-unsafe pair, as a diagnostic section.
        # Entries carry endpoint ids + the SAME 1500-char span the classifier
        # saw, so a reviewer can reproduce and score each verdict faithfully.
        # (Local artifact — ~/.genesis/output is never a public surface.)
        def _entry(ca: str, cb: str, v: dict, m: dict) -> dict:
            return {
                "source_id": m["source_id"],
                "target_id": m["target_id"],
                "verdict": v["relationship"],
                "confidence": v["confidence"],
                "reasoning": v.get("reasoning", ""),
                "stored_link_type": m["stored_link_type"],
                "strength": m["strength"],
                "content_a": ca[:1500],
                "content_b": cb[:1500],
            }

        joined = [
            (ca, cb, v, m)
            for (ca, cb), v, m in zip(pairs, verdicts, meta, strict=True)
            if not v.get("failed")
        ]
        rep_order = sorted(
            joined, key=lambda t: _stable_key((t[3]["source_id"], t[3]["target_id"]))
        )
        hand_score_sample = [_entry(ca, cb, v, m) for ca, cb, v, m in rep_order[:_HAND_SCORE_N]]
        unsafe_dump = [
            _entry(ca, cb, v, m) for ca, cb, v, m in joined if v["relationship"] != "distinct"
        ]

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        report_path = OUT_DIR / f"mw2_classifier_probe_{stamp}.json"
        report_path.write_text(
            json.dumps(
                {
                    "report": report,
                    "hand_score_sample": hand_score_sample,
                    "unsafe_dump": unsafe_dump,
                },
                indent=2,
            )
        )

        print("\n===== MW-2 CHECKPOINT A — classifier probe =====")
        print(json.dumps(report, indent=2))
        print(
            f"\nRepresentative hand-score sample ({len(hand_score_sample)}) + "
            f"unsafe dump ({len(unsafe_dump)}) + full report → {report_path}"
        )
        return 0
    finally:
        await db.close()


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {n}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=_positive_int, default=300, help="edges to sample")
    ap.add_argument("--batch", type=_positive_int, default=15, help="pairs per LLM call")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    return asyncio.run(run(args.n, args.batch, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
