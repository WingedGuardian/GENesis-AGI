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
# the similarity-derived (>=0.75) population MW-2 is about. (Not perfectly isolable
# from extraction's strength=0.7 or synthesis's strength=1.0 edges — we report the
# strength spread so the mix is visible.)
_SIMILARITY_TYPES = ("extends", "supports", "related_to")


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
        # Deterministic sample: seed sqlite's RANDOM via a stable ORDER key.
        rows = await db.execute_fetchall(
            "SELECT source_id, target_id, link_type, strength FROM memory_links "  # noqa: S608 - placeholders bound
            f"WHERE link_type IN ({','.join('?' * len(_SIMILARITY_TYPES))}) "
            "ORDER BY substr(source_id || target_id, 1, 12), source_id "
            "LIMIT ?",
            (*_SIMILARITY_TYPES, max(n * 3, n)),
        )
        rows = list(rows)
        # Stable pseudo-shuffle by seed, then take n (avoids RANDOM() nondeterminism).
        rows.sort(key=lambda r: hash((seed, r[0], r[1])) & 0xFFFFFFFF)
        rows = rows[:n]
        if not rows:
            print("No similarity edges found — nothing to probe.", file=sys.stderr)
            return 2

        ids = sorted({r[0] for r in rows} | {r[1] for r in rows})
        hydrated = await memory_crud.hydrate_for_expansion(db, ids)

        pairs: list[tuple[str, str]] = []
        meta: list[dict] = []
        for src_id, tgt_id, ltype, strength in rows:
            ca = (hydrated.get(src_id) or {}).get("content")
            cb = (hydrated.get(tgt_id) or {}).get("content")
            if ca and cb:
                pairs.append((ca, cb))
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
            verdicts.extend(await classify_relationships(router, chunk))
            print(
                f"  classified {min(i + batch, len(pairs))}/{len(pairs)} pairs...", file=sys.stderr
            )

        # ── tally ──────────────────────────────────────────────────────────
        dist = Counter(v["relationship"] for v in verdicts)
        total = len(verdicts)
        unsafe = dist["contradicts"] + dist["succeeded_by"] + dist["duplicate"]
        conf_by_rel: dict[str, list[float]] = {r: [] for r in COARSE_RELATIONSHIPS}
        for v in verdicts:
            conf_by_rel[v["relationship"]].append(v["confidence"])
        strengths = [m["strength"] for m in meta]

        def _mean(xs):
            return round(sum(xs) / len(xs), 3) if xs else 0.0

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "snapshot": str(SNAP),
            "sampled_edges": len(rows),
            "classified_pairs": total,
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

        # ── hand-scorable sample (all unsafe verdicts + a slice of distinct) ──
        sample = []
        distinct_shown = 0
        for (ca, cb), v, m in zip(pairs, verdicts, meta, strict=True):
            is_unsafe = v["relationship"] != "distinct"
            if is_unsafe or distinct_shown < 8:
                if not is_unsafe:
                    distinct_shown += 1
                sample.append(
                    {
                        "verdict": v["relationship"],
                        "confidence": v["confidence"],
                        "reasoning": v.get("reasoning", ""),
                        "stored_link_type": m["stored_link_type"],
                        "strength": m["strength"],
                        "content_a": ca[:400],
                        "content_b": cb[:400],
                    }
                )

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        report_path = OUT_DIR / f"mw2_classifier_probe_{stamp}.json"
        report_path.write_text(json.dumps({"report": report, "sample": sample}, indent=2))

        print("\n===== MW-2 CHECKPOINT A — classifier probe =====")
        print(json.dumps(report, indent=2))
        print(f"\nHand-scorable sample ({len(sample)} pairs) + full report → {report_path}")
        return 0
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="edges to sample")
    ap.add_argument("--batch", type=int, default=15, help="pairs per LLM call")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    return asyncio.run(run(args.n, args.batch, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
