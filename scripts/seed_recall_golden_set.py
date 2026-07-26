#!/usr/bin/env python3
"""Seed / manage the memory-integrity recall golden set.

Curates the install-local golden set the recall-health probe runs against:
  ~/.genesis/eval/golden/memory_integrity_recall.jsonl
(committed template lives in the repo; real cases are user data, never committed
— #1143 convention).

``--suggest`` MINES real recall telemetry (``eval_events`` recall_fired rows) for
STABLE query->memory pairs: the same top-1 memory was returned across >= 2
recalls with a healthy score, the memory still exists, and probe-sourced recalls
are excluded so the set never feeds on itself. Curation is a deliberate human
act — ``--suggest`` only prints; ``--accept-all`` / ``--add-line`` write.

Usage:
    python scripts/seed_recall_golden_set.py --suggest 20      # print candidates
    python scripts/seed_recall_golden_set.py --accept-all --suggest 20   # add them
    python scripts/seed_recall_golden_set.py --add-line '{"id":"x","query":"...","expected_memory_ids":["mid"]}'
    python scripts/seed_recall_golden_set.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_GOLDEN = Path.home() / ".genesis" / "eval" / "golden" / "memory_integrity_recall.jsonl"
# Real recall queries are near-unique prompts, so requiring an exact-query repeat
# finds almost nothing → accept singles too. And top_scores are RRF FUSION scores
# (tiny, ~0.03-0.07, NOT 0-1 similarity), so there is no meaningful absolute score
# gate — the top-1 per query is simply "what the pipeline ranked first," a valid
# drift anchor. Multi-recall pairs still rank first. Curation is the human's job.
_MIN_RECALLS = 1
_MIN_SCORE = 0.0


def _live_db_path() -> str:
    """Live genesis.db, home-anchored (GENESIS_DB_PATH override honored) — NOT
    the repo-anchored default that resolves to an empty worktree DB."""
    return os.environ.get("GENESIS_DB_PATH") or str(Path.home() / "genesis" / "data" / "genesis.db")


async def _suggest(n: int) -> list[dict]:
    """Mine eval_events for stable query->top-memory pairs (read-only)."""
    from genesis.db.connection import open_ro_connection

    # Exclude queries already in the golden set so the probe (which re-runs those
    # exact queries daily and emits recall_fired for them) can never feed the set
    # back into itself. This is the real self-feeding guard — `source` is a
    # collection selector, not a probe tag, so it can't distinguish probe events.
    _, existing_pairs = _load_existing()
    existing_queries = {q for (q, _mid) in existing_pairs}

    conn = await open_ro_connection(_live_db_path())
    try:
        rows = await conn.execute_fetchall(
            "SELECT metrics_json FROM eval_events "
            "WHERE dimension='memory' AND event_type='recall_fired' "
            "ORDER BY timestamp DESC LIMIT 5000"
        )
        # query -> {top1_id: (count, best_score)}
        agg: dict[str, dict[str, list]] = {}
        for (mj,) in rows:
            try:
                m = json.loads(mj)
            except (json.JSONDecodeError, TypeError):
                continue
            q = (m.get("query") or "").strip()
            if q in existing_queries:
                continue  # already a golden query — never re-mine (anti-feedback)
            ids = m.get("memory_ids") or []
            scores = m.get("top_scores") or []
            if not q or not ids or not scores:
                continue
            top_id, top_score = str(ids[0]), float(scores[0])
            if top_score < _MIN_SCORE:
                continue
            bucket = agg.setdefault(q, {})
            entry = bucket.setdefault(top_id, [0, 0.0])
            entry[0] += 1
            entry[1] = max(entry[1], top_score)

        # keep queries with a single dominant stable top-1
        candidates: list[dict] = []
        for q, bucket in agg.items():
            top_id, (count, best) = max(bucket.items(), key=lambda kv: kv[1][0])
            if count < _MIN_RECALLS:
                continue
            candidates.append({"query": q, "top_id": top_id, "count": count, "score": best})
        candidates.sort(key=lambda c: (c["count"], c["score"]), reverse=True)

        # verify the memory still exists
        verified: list[dict] = []
        for c in candidates:
            r = await conn.execute_fetchall(
                "SELECT 1 FROM memory_metadata WHERE memory_id = ? LIMIT 1", (c["top_id"],)
            )
            if r:
                verified.append(c)
            if len(verified) >= n:
                break
        return verified
    finally:
        await conn.close()


def _load_existing() -> tuple[list[dict], set[tuple[str, str]]]:
    if not _GOLDEN.exists():
        return [], set()
    cases, seen = [], set()
    for line in _GOLDEN.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            c = json.loads(s)
        except json.JSONDecodeError:
            continue
        cases.append(c)
        for e in c.get("expected_memory_ids") or []:
            seen.add((c.get("query", ""), str(e)))
    return cases, seen


def _append(cases: list[dict]) -> int:
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _, seen = _load_existing()
    added = 0
    with _GOLDEN.open("a") as fh:
        for c in cases:
            key = (c.get("query", ""), str((c.get("expected_memory_ids") or [""])[0]))
            if key in seen:
                continue
            fh.write(json.dumps(c) + "\n")
            seen.add(key)
            added += 1
    return added


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suggest", type=int, metavar="N", help="mine + print N candidate pairs")
    ap.add_argument("--accept-all", action="store_true", help="append the suggested candidates")
    ap.add_argument("--add-line", metavar="JSON", help="append one hand-written case")
    ap.add_argument("--list", action="store_true", help="print the current golden set")
    args = ap.parse_args()

    if args.list:
        cases, _ = _load_existing()
        print(f"{len(cases)} case(s) in {_GOLDEN}")
        for c in cases:
            print(
                f"  {c.get('id', '')}: {c.get('query', '')[:60]} -> {c.get('expected_memory_ids')}"
            )
        return 0

    if args.add_line:
        c = json.loads(args.add_line)
        if not c.get("query") or not c.get("expected_memory_ids"):
            print("ERROR: case needs query + expected_memory_ids", file=sys.stderr)
            return 2
        c.setdefault("id", f"manual-{abs(hash(c['query'])) % 10**8}")
        print(f"added {_append([c])} case(s)")
        return 0

    if args.suggest:
        cands = await _suggest(args.suggest)
        cases = [
            {
                "id": f"mined-{i}",
                "query": c["query"],
                "expected_memory_ids": [c["top_id"]],
                "notes": f"stable: top-1 across {c['count']} recalls, best score {c['score']:.2f}",
            }
            for i, c in enumerate(cands)
        ]
        if args.accept_all:
            print(f"added {_append(cases)} case(s) to {_GOLDEN}")
        else:
            for case in cases:
                print(json.dumps(case))
            print(
                f"\n# {len(cases)} candidate(s). Re-run with --accept-all to add, "
                f"or copy the good lines into {_GOLDEN}",
                file=sys.stderr,
            )
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
