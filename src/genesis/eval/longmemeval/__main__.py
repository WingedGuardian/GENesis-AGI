"""CLI: ``python -m genesis.eval.longmemeval [options]``.

Runs the LongMemEval oracle harness and prints per-arm, per-question-type
accuracy. Persists one ``eval_runs`` row per arm (``model_profile=
longmemeval:<arm>``) unless ``--no-persist`` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from genesis.eval.longmemeval.cli import DEFAULT_DATASET, execute, print_report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="genesis.eval.longmemeval")
    p.add_argument("--dataset-path", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=None, help="only run the first N questions")
    p.add_argument("--k", type=int, default=10, help="recall top-K")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--no-rerank",
        action="store_true",
        help="only run the two non-rerank arms (skip Voyage reranking)",
    )
    p.add_argument("--no-persist", action="store_true", help="do not write eval_runs")
    p.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="results DB (default: production genesis.db); a fresh path is migrated",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summaries = asyncio.run(
        execute(
            dataset_path=args.dataset_path,
            limit=args.limit,
            k=args.k,
            concurrency=args.concurrency,
            no_rerank=args.no_rerank,
            persist=not args.no_persist,
            db_path=args.db_path,
        ),
    )
    print_report(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
