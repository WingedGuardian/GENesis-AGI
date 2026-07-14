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
from genesis.eval.longmemeval.dataset import filter_by_types, load_oracle
from genesis.eval.longmemeval.runner import run_longmemeval, select_arms

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
        attempted = s.total_cases - s.skipped_cases
        skip_note = f"  skipped={s.skipped_cases}" if s.skipped_cases else ""
        print(
            f"\n[{arm_label}]  overall={s.aggregate_score}  "
            f"attempted={attempted}/{s.total_cases}{skip_note}",
        )
        headline = (
            "evidence_recall_rate",
            "evidence_coverage_mean",
            "evidence_coverage_final_mean",
        )
        for metric in headline:
            val = s.scores.get(metric)
            if val is not None:
                print(f"    {metric}: {val}")
        # Graph-arm ingest/expansion volume lives in metadata, not scores.
        for metric in ("links_created_mean", "expanded_mean"):
            val = s.metadata.get(metric)
            if val is not None:
                print(f"    {metric}: {val}")
        for qtype, acc in sorted(s.scores.items()):
            if qtype in headline:
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
    types: str | None = None,
    dump_dir: Path | None = None,
    graph: bool = False,
    graph_link_threshold: float | None = None,
) -> dict[str, EvalRunSummary]:
    """Load the dataset, run the harness, persist (optionally), return summaries.

    ``types`` (comma-separated question types) filters BEFORE ``limit`` so
    ``--types temporal-reasoning --limit 20`` means "the first 20 temporal
    questions", not "temporal questions among the first 20".

    ``graph`` ADDS a ``+graph`` variant of every selected arm (paired
    baseline-vs-graph comparison in one run, one dump dir).
    """
    load_secrets()
    instances = load_oracle(dataset_path)
    if types:
        instances = filter_by_types(instances, types)
    if limit:
        instances = instances[:limit]
    logger.info("loaded %d questions from %s", len(instances), dataset_path)

    arms = select_arms(no_rerank=no_rerank, graph=graph)
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
            dump_dir=dump_dir,
            link_threshold=graph_link_threshold,
        )
    finally:
        if db is not None:
            await db.close()
