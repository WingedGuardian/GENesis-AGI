"""Shared CLI logic for the LongMemEval harness.

Used by both ``python -m genesis.eval.longmemeval`` (``__main__``) and the
``genesis eval longmemeval`` subcommand, so orchestration + reporting live in
one place.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.eval.longmemeval.client import load_secrets
from genesis.eval.longmemeval.dataset import load_oracle
from genesis.eval.longmemeval.runner import default_arms, run_longmemeval

if TYPE_CHECKING:
    from genesis.eval.types import EvalRunSummary

logger = logging.getLogger("genesis.eval.longmemeval")

DEFAULT_DATASET = Path.home() / "tmp" / "longmemeval" / "longmemeval_oracle.json"


def build_reranker():
    """Build a VoyageReranker (enabled iff API_KEY_VOYAGE is present)."""
    from genesis.memory.reranker import VoyageReranker

    rr = VoyageReranker()
    if not getattr(rr, "enabled", False):
        logger.warning("VoyageReranker disabled (no API_KEY_VOYAGE); rerank arms inert")
    return rr


def print_report(summaries: dict[str, EvalRunSummary]) -> None:
    print("\n===== LongMemEval results (per arm) =====")
    for arm_label, s in summaries.items():
        print(f"\n[{arm_label}]  overall={s.aggregate_score}  n={s.total_cases}")
        ev = s.scores.get("evidence_recall_rate")
        if ev is not None:
            print(f"    evidence_recall_rate: {ev}")
        for qtype, acc in sorted(s.scores.items()):
            if qtype == "evidence_recall_rate":
                continue
            print(f"    {qtype}: {acc}")
        cost = sum(r.cost_usd for r in s.results)
        print(f"    LLM cost (answer+judge): ${cost:.4f}")


async def execute(
    *,
    dataset_path: Path,
    limit: int | None = None,
    k: int = 10,
    concurrency: int = 4,
    no_rerank: bool = False,
    persist: bool = True,
    db_path: Path | None = None,
) -> dict[str, EvalRunSummary]:
    """Load the dataset, run the harness, persist (optionally), return summaries."""
    load_secrets()
    instances = load_oracle(dataset_path)
    if limit:
        instances = instances[:limit]
    logger.info("loaded %d questions from %s", len(instances), dataset_path)

    arms = default_arms()
    if no_rerank:
        arms = [a for a in arms if not a.rerank]
    reranker = None if no_rerank else build_reranker()

    db = None
    if persist:
        from genesis.db.connection import get_db, init_db
        from genesis.env import genesis_db_path

        if db_path is not None:
            # Fresh standalone results DB: create_all_tables + migrations
            # (eval_runs is migration-only, not in create_all_tables).
            from genesis.db.migrations.runner import MigrationRunner

            db = await init_db(db_path)
            await MigrationRunner(db).run_pending()
        else:
            db = await get_db(genesis_db_path())

    try:
        return await run_longmemeval(
            instances,
            db=db,
            arms=arms,
            k=k,
            concurrency=concurrency,
            reranker=reranker,
        )
    finally:
        if db is not None:
            await db.close()
