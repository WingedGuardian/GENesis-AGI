"""Eval harness CLI — `python -m genesis eval` subcommands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from genesis.eval.datasets import list_datasets
from genesis.eval.types import EvalTrigger


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register `genesis eval` on an existing subparser group."""
    eval_cmd = subparsers.add_parser(
        "eval",
        help="Model evaluation harness",
        description="Run automated model evaluations against golden datasets.",
    )
    eval_sub = eval_cmd.add_subparsers(dest="eval_command")

    # -- eval run --
    run_cmd = eval_sub.add_parser("run", help="Run eval against a provider")
    run_cmd.add_argument(
        "--model", "-m", required=True,
        help="Provider name from model_routing.yaml (e.g. cerebras-qwen)",
    )
    run_cmd.add_argument(
        "--dataset", "-d", required=True,
        help="Dataset name (without .yaml), or 'all' for all datasets",
    )
    run_cmd.add_argument(
        "--system-prompt", "-s",
        help="Optional system prompt override",
    )
    run_cmd.add_argument(
        "--no-db", action="store_true",
        help="Skip storing results in the database",
    )

    # -- eval results --
    results_cmd = eval_sub.add_parser("results", help="Show recent eval results")
    results_cmd.add_argument(
        "--model", "-m",
        help="Filter by provider name",
    )
    results_cmd.add_argument(
        "--dataset", "-d",
        help="Filter by dataset name",
    )
    results_cmd.add_argument(
        "--last", "-n", type=int, default=5,
        help="Number of recent runs to show (default: 5)",
    )

    # -- eval datasets --
    eval_sub.add_parser("datasets", help="List available eval datasets")

    eval_cmd.set_defaults(func=_run_eval_cli)


def _run_eval_cli(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.eval_command == "run":
        return asyncio.run(_cmd_run(args))
    elif args.eval_command == "results":
        return asyncio.run(_cmd_results(args))
    elif args.eval_command == "datasets":
        return _cmd_datasets()
    else:
        print("usage: genesis eval {run|results|datasets}", file=sys.stderr)
        return 1


async def _cmd_run(args: argparse.Namespace) -> int:
    from genesis.eval.runner import run_eval

    db = None
    if not args.no_db:
        try:
            import aiosqlite  # noqa: I001

            from genesis.env import genesis_db_path
            db = await aiosqlite.connect(str(genesis_db_path()))
        except Exception as e:
            print(f"warning: could not open DB ({e}), results won't be stored")

    try:
        datasets = (
            list_datasets() if args.dataset == "all"
            else [args.dataset]
        )

        for ds_name in datasets:
            print(f"\n{'='*60}")
            print(f"  Eval: {args.model} on {ds_name}")
            print(f"{'='*60}\n")

            summary = await run_eval(
                provider_name=args.model,
                dataset_name=ds_name,
                trigger=EvalTrigger.MANUAL,
                db=db,
                system_prompt=args.system_prompt,
            )

            _print_summary(summary)

    finally:
        if db is not None:
            await db.close()
    return 0


async def _cmd_results(args: argparse.Namespace) -> int:
    try:
        import aiosqlite  # noqa: I001

        from genesis.env import genesis_db_path
        from genesis.eval.db import get_runs

        async with aiosqlite.connect(str(genesis_db_path())) as db:
            runs = await get_runs(
                db, model_id=args.model, dataset=args.dataset, limit=args.last,
            )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not runs:
        print("no eval runs found")
        return 0

    for run in runs:
        total = run.get("total_cases", 0)
        passed = run.get("passed_cases", 0)
        pct = (passed / total * 100) if total > 0 else 0
        print(
            f"  {run.get('created_at', '?')[:19]}  "
            f"{run.get('model_id', '?'):30s}  "
            f"{run.get('dataset', '?'):20s}  "
            f"{passed}/{total} ({pct:.0f}%)  "
            f"{run.get('duration_s', 0):.1f}s"
        )
    return 0


def _cmd_datasets() -> int:
    names = list_datasets()
    if not names:
        print("no datasets found in config/eval_datasets/")
        return 0
    print("available datasets:")
    for name in names:
        print(f"  {name}")
    return 0


def _print_summary(summary) -> None:
    """Pretty-print an EvalRunSummary."""
    pct = summary.aggregate_score * 100
    print(f"  Run ID:   {summary.run_id[:12]}")
    print(f"  Model:    {summary.model_id} ({summary.model_profile})")
    print(f"  Dataset:  {summary.dataset}")
    print(f"  Results:  {summary.passed_cases}/{summary.total_cases} passed ({pct:.0f}%)")
    if summary.skipped_cases:
        print(f"  Skipped:  {summary.skipped_cases}")
    print(f"  Duration: {summary.duration_s:.1f}s")
    print()

    # Per-case breakdown
    for r in summary.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"    [{status}] {r.case_id}", end="")
        if r.scorer_detail:
            print(f"  -- {r.scorer_detail}", end="")
        if r.latency_ms > 0:
            print(f"  ({r.latency_ms:.0f}ms)", end="")
        print()
