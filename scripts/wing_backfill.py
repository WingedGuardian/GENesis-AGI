#!/usr/bin/env python3
"""One-shot supervised wing backfill for legacy general/uncategorized memories.

The live store path auto-classifies wings at store time (``MemoryStore.store``
→ ``taxonomy.classify``), but ~2.9K rows predate that wiring — most predate
the user-work wings (``dev_workflow`` etc.) added 2026-05-11 — and sit in
``general``/NULL where wing-filtered recall can't reach them.

Two-stage classification, mirroring the layered design of ``taxonomy.classify``:

1. **Deterministic:** ``taxonomy.classify()`` — accepted at confidence >= 0.6
   (path/keyword/tag layers). Free, and validated against a live sample.
2. **LLM batch:** remaining rows go through the router call site
   ``wing_backfill`` (free background chains) in batches; the model picks a
   wing from the closed WINGS list. Invalid/unsure answers leave the row
   untouched (do-no-harm).

Writes go to BOTH stores or neither (cross-store mirror discipline):
``memory_metadata.wing/room`` in SQLite, and ``wing``/``room``/``life_domain``
on the Qdrant payload for embedded rows (``fts5_only`` rows have no point).
On a Qdrant failure the SQLite row is reverted and the id logged.

Dry-run by default; ``--apply`` performs the writes. Idempotent: reclassified
rows drop out of the backlog WHERE clause.

Usage:
    python scripts/wing_backfill.py --sample 30          # dry-run a sample
    python scripts/wing_backfill.py                      # dry-run everything
    python scripts/wing_backfill.py --apply              # supervised write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
from pathlib import Path

# Ensure genesis package is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

from genesis.env import secrets_path

logger = logging.getLogger("wing_backfill")

CALL_SITE_ID = "wing_backfill"
LLM_BATCH_SIZE = 25
CONTENT_SNIPPET_CHARS = 300
TAXONOMY_ACCEPT_CONFIDENCE = 0.6


def build_batch_prompt(rows: list[dict], wings: list[str]) -> str:
    """Build the classification prompt for one LLM batch."""
    lines = [
        "Classify each memory snippet below into exactly one wing (topic domain).",
        f"Valid wings: {', '.join(wings)}.",
        "These memories are from an AI system (Genesis) working on its own codebase.",
        "Classify by SUBJECT MATTER, not by activity type:",
        "- memory: recall/storage/embeddings/FTS/Qdrant/knowledge-base subsystem",
        "- learning: skills, reflection, calibration, distillation, evaluation subsystem",
        "- routing: LLM model selection, providers, API keys, budgets, circuit breakers",
        "- infrastructure: servers, systemd, deploy, disk, containers, hosts, DBs as infra",
        "- channels: telegram, dashboard/web UI, voice, browser automation, messaging",
        "- autonomy: task executor, ego, automatons, autonomous sessions",
        "- dev_workflow: git/PR/CI/worktree/merge MECHANICS themselves (not the code",
        "  being changed — a memory about investigating memory-subsystem code is",
        "  'memory', not dev_workflow)",
        "- research: evaluating external tools/articles/papers/services",
        "- integrations: wiring external APIs/services into the system",
        "- career / employment: the user's job hunt or job work",
        "Use 'general' ONLY if genuinely unclassifiable.",
        "",
        'Respond with ONLY a JSON object mapping id to wing, e.g. {"a1b2": "memory"}.',
        "",
    ]
    for row in rows:
        snippet = (row["content"] or "").replace("\n", " ")[:CONTENT_SNIPPET_CHARS]
        lines.append(f"{row['short_id']}: {snippet}")
    return "\n".join(lines)


def parse_llm_response(text: str, expected_ids: set[str], wings: frozenset[str]) -> dict[str, str]:
    """Parse the model's JSON id->wing mapping, keeping only valid entries.

    Do-no-harm: unknown ids, invalid wings, and 'general' verdicts are dropped
    (the row simply stays unclassified).
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        raw = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        wing = value.strip().lower()
        if key in expected_ids and wing in wings and wing != "general":
            result[key] = wing
    return result


def default_room(wing: str) -> str:
    """Default room for a wing — same convention as taxonomy's tag layer."""
    from genesis.memory.taxonomy import ROOMS

    rooms = ROOMS.get(wing)
    return rooms[0] if rooms else "uncategorized"


async def fetch_backlog(db, limit: int | None, sample: int | None) -> list[dict]:
    """Read the general/NULL-wing backlog joined with FTS content."""
    query = """
        SELECT m.memory_id, m.collection, m.embedding_status, f.content, f.tags
        FROM memory_metadata m JOIN memory_fts f ON f.memory_id = m.memory_id
        WHERE (m.wing IS NULL OR m.wing = '' OR m.wing = 'general')
          AND m.deprecated = 0
        ORDER BY m.created_at
    """
    cursor = await db.execute(query)
    rows = [
        {
            "memory_id": r[0],
            "collection": r[1],
            "embedding_status": r[2],
            "content": r[3],
            "tags": r[4],
            "short_id": r[0][:8],
        }
        for r in await cursor.fetchall()
    ]
    if sample:
        random.seed(42)  # reproducible sample across dry-run and review
        rows = random.sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    return rows


def parse_tags(raw: str | None) -> list[str]:
    """Best-effort tag parse — FTS stores JSON lists or comma strings."""
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return [str(t) for t in loaded]
    except (json.JSONDecodeError, TypeError):
        pass
    return [t.strip() for t in raw.split(",") if t.strip()]


def stage1_taxonomy(rows: list[dict]) -> tuple[dict[str, tuple[str, str]], list[dict]]:
    """Run taxonomy.classify over rows; return accepted {id: (wing, room)} + remainder."""
    from genesis.memory.taxonomy import classify

    accepted: dict[str, tuple[str, str]] = {}
    remainder: list[dict] = []
    for row in rows:
        result = classify(row["content"] or "", tags=parse_tags(row["tags"]))
        if result.wing != "general" and result.confidence >= TAXONOMY_ACCEPT_CONFIDENCE:
            accepted[row["memory_id"]] = (result.wing, result.room)
        else:
            remainder.append(row)
    return accepted, remainder


async def stage2_llm(router, rows: list[dict]) -> dict[str, tuple[str, str]]:
    """Batch-classify the taxonomy remainder through the router."""
    from genesis.memory.taxonomy import WINGS

    wings_sorted = sorted(WINGS)
    accepted: dict[str, tuple[str, str]] = {}
    for start in range(0, len(rows), LLM_BATCH_SIZE):
        batch = rows[start : start + LLM_BATCH_SIZE]
        short_to_full = {r["short_id"]: r["memory_id"] for r in batch}
        prompt = build_batch_prompt(batch, wings_sorted)
        try:
            result = await router.route_call(
                CALL_SITE_ID,
                [{"role": "user", "content": prompt}],
                suppress_dead_letter=True,
            )
        except Exception:
            logger.exception("LLM batch %d failed — rows stay unclassified", start)
            continue
        if not getattr(result, "success", True):
            logger.error(
                "LLM batch %d routing failed (%s) — rows stay unclassified",
                start,
                getattr(result, "error", "unknown error"),
            )
            continue
        content = getattr(result, "content", None) or ""
        verdicts = parse_llm_response(content, set(short_to_full), WINGS)
        if not verdicts and batch:
            logger.warning("LLM batch %d returned unparseable content: %.200r", start, content)
        for short_id, wing in verdicts.items():
            accepted[short_to_full[short_id]] = (wing, default_room(wing))
        logger.info(
            "LLM batch %d-%d: %d/%d classified",
            start,
            start + len(batch),
            len(verdicts),
            len(batch),
        )
    return accepted


async def apply_updates(
    db,
    qdrant,
    rows_by_id: dict[str, dict],
    classifications: dict[str, tuple[str, str]],
) -> tuple[int, int]:
    """Write wing/room to SQLite + Qdrant payload. Returns (applied, failed).

    SQLite first, then Qdrant; a Qdrant failure reverts the SQLite row so the
    two stores never diverge (cross-store mirror discipline).
    """
    from genesis.memory.taxonomy import classify_life_domain
    from genesis.qdrant.collections import update_payload

    applied = failed = 0
    for memory_id, (wing, room) in classifications.items():
        row = rows_by_id[memory_id]
        await db.execute(
            "UPDATE memory_metadata SET wing = ?, room = ? "
            "WHERE memory_id = ? "
            "AND (wing IS NULL OR wing = '' OR wing = 'general') "
            "AND deprecated = 0",
            (wing, room, memory_id),
        )
        await db.commit()
        if row["embedding_status"] == "embedded":
            try:
                update_payload(
                    qdrant,
                    collection=row["collection"],
                    point_id=memory_id,
                    payload={
                        "wing": wing,
                        "room": room,
                        "life_domain": classify_life_domain(wing),
                    },
                )
            except Exception:
                logger.exception(
                    "Qdrant payload update failed for %s — reverting SQLite row",
                    memory_id,
                )
                await db.execute(
                    "UPDATE memory_metadata SET wing = 'general', "
                    "room = 'uncategorized' WHERE memory_id = ?",
                    (memory_id,),
                )
                await db.commit()
                failed += 1
                continue
        applied += 1
    return applied, failed


def build_router(db):
    """Standalone Router — same pattern as scripts/backfill_session_memories.py."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.config import load_config
    from genesis.routing.cost_tracker import CostTracker
    from genesis.routing.degradation import DegradationTracker
    from genesis.routing.litellm_delegate import LiteLLMDelegate
    from genesis.routing.router import Router

    # Script-relative so the call site resolves from the same checkout the
    # script runs from (worktree pre-merge, ~/genesis in production).
    config_path = Path(__file__).resolve().parent.parent / "config" / "model_routing.yaml"
    config = load_config(config_path)
    return Router(
        config=config,
        breakers=CircuitBreakerRegistry(config.providers),
        cost_tracker=CostTracker(db=db),
        degradation=DegradationTracker(),
        delegate=LiteLLMDelegate(config),
    )


async def main(args: argparse.Namespace) -> int:
    import aiosqlite

    from genesis.env import genesis_db_path

    load_dotenv(secrets_path())

    db = await aiosqlite.connect(genesis_db_path())
    db.row_factory = aiosqlite.Row  # CRUD layer (cost tracker budgets) needs dict(row)
    await db.execute("PRAGMA busy_timeout = 15000")
    try:
        rows = await fetch_backlog(db, args.limit, args.sample)
        logger.info("backlog rows in scope: %d", len(rows))
        if not rows:
            return 0
        rows_by_id = {r["memory_id"]: r for r in rows}

        stage1, remainder = stage1_taxonomy(rows)
        logger.info(
            "stage 1 (taxonomy >= %.1f): %d classified, %d remain",
            TAXONOMY_ACCEPT_CONFIDENCE,
            len(stage1),
            len(remainder),
        )

        stage2: dict[str, tuple[str, str]] = {}
        if remainder and not args.skip_llm:
            router = build_router(db)
            stage2 = await stage2_llm(router, remainder)
            logger.info("stage 2 (LLM): %d classified", len(stage2))

        classifications = {**stage1, **stage2}
        for memory_id, (wing, room) in sorted(classifications.items()):
            snippet = (rows_by_id[memory_id]["content"] or "")[:110].replace("\n", " ")
            stage = "taxo" if memory_id in stage1 else "llm"
            print(f"{memory_id[:8]} [{stage}] -> {wing}/{room}  |  {snippet}")
        unclassified = len(rows) - len(classifications)
        print(
            f"\nTOTAL: {len(classifications)}/{len(rows)} classified "
            f"({len(stage1)} taxonomy, {len(stage2)} LLM), {unclassified} stay general"
        )

        if not args.apply:
            print("\nDRY RUN — no writes. Re-run with --apply to write.")
            return 0

        from qdrant_client import QdrantClient

        from genesis.env import qdrant_url

        qdrant = QdrantClient(url=qdrant_url(), timeout=10)
        applied, failed = await apply_updates(db, qdrant, rows_by_id, classifications)
        print(
            f"APPLIED: {applied} rows updated in both stores, {failed} reverted on Qdrant failure"
        )
        return 0 if failed == 0 else 1
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="cap rows processed")
    parser.add_argument(
        "--sample", type=int, default=None, help="random sample of N rows (seed 42)"
    )
    parser.add_argument("--skip-llm", action="store_true", help="stage 1 (taxonomy) only")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(parser.parse_args())))
