"""Eval harness CLI — `python -m genesis eval` subcommands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from genesis.db.connection import BUSY_TIMEOUT_MS
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

    # -- eval benchmark --
    bench_cmd = eval_sub.add_parser(
        "benchmark",
        help="Run all enabled providers across all datasets (comparison table; "
             "for the Genesis-vs-bare A/B see `bench`)",
    )
    bench_cmd.add_argument(
        "--include-paid", action="store_true",
        help="Include paid (non-free) providers",
    )
    bench_cmd.add_argument(
        "--model", "-m",
        help="Run only this provider (single-provider mode)",
    )
    bench_cmd.add_argument(
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

    # -- eval compare --
    compare_cmd = eval_sub.add_parser(
        "compare",
        help="Show comparison table from stored results (no re-running)",
    )
    compare_cmd.add_argument(
        "--last", "-n", type=int, default=1,
        help="Most recent N runs per provider/dataset (default: 1)",
    )

    # -- eval export --
    export_cmd = eval_sub.add_parser(
        "export",
        help="Export benchmark results as markdown",
    )
    export_cmd.add_argument(
        "-o", "--output",
        help="Write to file instead of stdout",
    )

    # -- eval datasets --
    eval_sub.add_parser("datasets", help="List available eval datasets")

    # -- eval gauntlet --
    gauntlet_cmd = eval_sub.add_parser(
        "gauntlet",
        help="Run the agentic model-roster gauntlet (drive CC through a fix-loop)",
        description=(
            "Validate a roster model by having Claude Code, driving that model, "
            "fix broken Python fixtures scored by pytest. Advisory only."
        ),
    )
    gauntlet_cmd.add_argument(
        "--model", "-m", required=True,
        help="Roster model name (e.g. claude, glm-5.2)",
    )
    gauntlet_cmd.add_argument(
        "--no-db", action="store_true",
        help="Skip storing results in the database",
    )
    gauntlet_cmd.add_argument(
        "--check-regression", action="store_true",
        help="Also run the advisory PASS->FAIL regression check (files a proposal)",
    )

    # -- eval bench (A/B) --
    ab_cmd = eval_sub.add_parser(
        "bench",
        help="Genesis-vs-bare-Claude A/B bench on real tasks (WS-1 A3)",
        description=(
            "Run the paired task bench: a cognition-enabled Genesis arm "
            "(identity + read-only memory recall against a DB snapshot) vs a "
            "bare Claude Code arm (no Genesis context), judged per-arm against "
            "each task's ex-ante criteria. NOT `benchmark` — that is the "
            "cross-provider dataset comparison table."
        ),
    )
    ab_cmd.add_argument(
        "--tasks", default=None,
        help="Task JSONL path (default: ~/.genesis/eval/bench_tasks_v1.jsonl; "
             "must live OUTSIDE the repo)",
    )
    ab_cmd.add_argument(
        "--model", "-m", default="sonnet",
        help="CC model for BOTH arms (default: sonnet)",
    )
    ab_cmd.add_argument(
        "--effort", default="medium",
        help="CC effort for BOTH arms (default: medium)",
    )
    ab_cmd.add_argument(
        "--limit", type=int, default=None, help="Run only the first N tasks",
    )
    ab_cmd.add_argument(
        "--task-id", default=None, help="Run a single task by id (shakedowns)",
    )
    ab_cmd.add_argument(
        "--epsilon", type=float, default=0.05,
        help="Score-difference tie band for the win-rate (default: 0.05)",
    )
    ab_cmd.add_argument(
        "--no-db", action="store_true",
        help="Skip persisting paired eval_runs rows",
    )
    ab_cmd.add_argument(
        "--keep-workdir", action="store_true",
        help="Keep ~/tmp/bench/<run_id> (snapshot, arm workdirs, transcripts)",
    )
    ab_cmd.add_argument(
        "--no-verify-prod", action="store_true",
        help="Skip the prod-delta isolation probe (NOT recommended)",
    )
    ab_cmd.add_argument(
        "--judge-provider", default=None,
        help="Start the judge chain at this routing provider (e.g. "
             "openrouter-deepseek-v4 when the free NIM tier is down)",
    )

    # -- eval longmemeval (external memory benchmark, WS-1 A4) --
    lme_cmd = eval_sub.add_parser(
        "longmemeval",
        help="LongMemEval external memory benchmark (WS-1 A4)",
        description=(
            "Load each LongMemEval question's haystack into a fresh ephemeral "
            "Genesis memory store (zero prod contact), recall + answer, and "
            "grade with the standard gpt-4o judge. Reports per-arm (raw vs "
            "keyword query x rerank) per-question-type accuracy."
        ),
    )
    lme_cmd.add_argument(
        "--dataset-path", default=None,
        help="oracle JSON (default: ~/tmp/longmemeval/longmemeval_oracle.json)",
    )
    lme_cmd.add_argument("--limit", type=int, default=None, help="first N questions")
    lme_cmd.add_argument("--k", type=int, default=10, help="recall top-K")
    lme_cmd.add_argument("--concurrency", type=int, default=4)
    lme_cmd.add_argument(
        "--no-rerank", action="store_true", help="skip the Voyage rerank arms",
    )
    lme_cmd.add_argument(
        "--no-persist", action="store_true", help="do not write eval_runs",
    )
    lme_cmd.add_argument(
        "--db-path", default=None,
        help="results DB (default: production genesis.db; a fresh path is migrated)",
    )
    lme_cmd.add_argument(
        "--types", default=None,
        help="comma-separated question types to run (filtered before --limit)",
    )
    lme_cmd.add_argument(
        "--dump-dir", default=None,
        help="write per-question diagnostics (one <arm>.jsonl per arm) here",
    )
    lme_cmd.add_argument(
        "--graph", action="store_true",
        help="ADD a +graph variant of every selected arm (linked store + expansion)",
    )
    lme_cmd.add_argument(
        "--graph-link-threshold", type=float, default=None,
        help="cosine threshold for auto-linking on the graph arm's store "
        "(default: the prod linker default, 0.75)",
    )

    eval_cmd.set_defaults(func=_run_eval_cli)


def _load_secrets() -> None:
    """Load secrets.env so API keys are available for standalone CLI use."""
    try:
        from dotenv import load_dotenv

        from genesis.env import secrets_path
        path = secrets_path()
        if path.is_file():
            load_dotenv(str(path), override=True)
    except Exception:
        pass  # Fail silently — server may have already loaded them


def _run_eval_cli(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    _load_secrets()

    if args.eval_command == "run":
        return asyncio.run(_cmd_run(args))
    elif args.eval_command == "benchmark":
        return asyncio.run(_cmd_benchmark(args))
    elif args.eval_command == "results":
        return asyncio.run(_cmd_results(args))
    elif args.eval_command == "compare":
        return asyncio.run(_cmd_compare(args))
    elif args.eval_command == "export":
        return asyncio.run(_cmd_export(args))
    elif args.eval_command == "datasets":
        return _cmd_datasets()
    elif args.eval_command == "gauntlet":
        return asyncio.run(_cmd_gauntlet(args))
    elif args.eval_command == "bench":
        return asyncio.run(_cmd_bench(args))
    elif args.eval_command == "longmemeval":
        return asyncio.run(_cmd_longmemeval(args))
    else:
        print(
            "usage: genesis eval {run|benchmark|results|compare|export|datasets"
            "|gauntlet|bench|longmemeval}",
            file=sys.stderr,
        )
        return 1


async def _cmd_longmemeval(args: argparse.Namespace) -> int:
    from pathlib import Path

    from genesis.eval.longmemeval.cli import DEFAULT_DATASET, execute, print_report

    summaries = await execute(
        dataset_path=Path(args.dataset_path) if args.dataset_path else DEFAULT_DATASET,
        limit=args.limit,
        k=args.k,
        concurrency=args.concurrency,
        no_rerank=args.no_rerank,
        persist=not args.no_persist,
        db_path=Path(args.db_path) if args.db_path else None,
        types=args.types,
        dump_dir=Path(args.dump_dir) if args.dump_dir else None,
        graph=args.graph,
        graph_link_threshold=args.graph_link_threshold,
    )
    print_report(summaries)
    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    from genesis.eval.runner import run_eval

    db = None
    if not args.no_db:
        try:
            import aiosqlite  # noqa: I001

            from genesis.env import genesis_db_path
            db = await aiosqlite.connect(str(genesis_db_path()))
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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


async def _cmd_gauntlet(args: argparse.Namespace) -> int:
    from genesis.cc.roster import RosterError
    from genesis.eval.gauntlet import GauntletBusyError, run_gauntlet

    db = None
    if not args.no_db:
        try:
            import aiosqlite  # noqa: I001

            from genesis.env import genesis_db_path
            db = await aiosqlite.connect(str(genesis_db_path()))
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except Exception as e:
            print(f"warning: could not open DB ({e}), results won't be stored")

    summary = None
    try:
        print(f"\n{'='*60}")
        print(f"  Gauntlet: {args.model}")
        print(f"{'='*60}\n")
        summary = await run_gauntlet(args.model, db=db, trigger=EvalTrigger.MANUAL)
        _print_summary(summary)
        for r in summary.results:
            print(f"  - {r.case_id}: {r.scorer_detail}")
        if db is not None and args.check_regression:
            from genesis.eval.gauntlet_regression import check_gauntlet_regression

            reg = await check_gauntlet_regression(db, summary, outreach_pipeline=None)
            if reg:
                print(f"\n  ⚠ regression surfaced (proposal {reg['proposal_id']})")
    except RosterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except GauntletBusyError as e:
        print(f"busy: {e}", file=sys.stderr)
        return 3
    finally:
        if db is not None:
            await db.close()
    # Non-zero exit only on a GENUINE failure (skips are infra, not failures).
    return 1 if summary is not None and summary.failed_cases > 0 else 0


async def _cmd_bench(args: argparse.Namespace) -> int:
    from genesis.cc.types import CCModel, EffortLevel
    from genesis.eval.bench.report import render_console
    from genesis.eval.bench.runner import BenchBusyError, run_bench
    from genesis.eval.bench.tasks import TaskFileError

    try:
        model = CCModel(args.model)
        effort = EffortLevel(args.effort)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    db = None
    if not args.no_db:
        try:
            import aiosqlite  # noqa: I001

            from genesis.env import genesis_db_path
            db = await aiosqlite.connect(str(genesis_db_path()))
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except Exception as e:
            print(f"warning: could not open DB ({e}), results won't be stored")

    try:
        report = await run_bench(
            tasks_path=args.tasks,
            model=model,
            effort=effort,
            limit=args.limit,
            task_id=args.task_id,
            epsilon=args.epsilon,
            db=db,
            keep_workdir=args.keep_workdir,
            verify_prod=not args.no_verify_prod,
            judge_provider=args.judge_provider,
        )
        print(render_console(report))
    except TaskFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BenchBusyError as e:
        print(f"busy: {e}", file=sys.stderr)
        return 3
    finally:
        if db is not None:
            await db.close()
    return 0


async def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Run all enabled providers across all datasets and print a comparison table."""
    from pathlib import Path

    from genesis.eval.runner import run_eval
    from genesis.routing.config import load_config

    config_path = Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"
    config = load_config(config_path)
    datasets = list_datasets()

    if not datasets:
        print("no eval datasets found in config/eval_datasets/")
        return 1

    # Determine which providers to benchmark
    if args.model:
        if args.model not in config.providers:
            print(f"error: unknown provider '{args.model}'", file=sys.stderr)
            return 1
        providers = [args.model]
    else:
        providers = [
            name for name, cfg in sorted(config.providers.items())
            if getattr(cfg, "enabled", True)  # skip explicitly disabled
            and getattr(cfg, "has_api_key", True)  # skip keyless — eval would 401
            and (args.include_paid or cfg.is_free)
        ]

    if not providers:
        print("no matching providers found (use --include-paid to include paid providers)")
        return 1

    db = None
    if not args.no_db:
        try:
            import aiosqlite  # noqa: I001
            from genesis.env import genesis_db_path
            db = await aiosqlite.connect(str(genesis_db_path()))
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except Exception as e:
            print(f"warning: could not open DB ({e}), results won't be stored")

    # results[provider][dataset] = (passed, attempted, skipped)
    results: dict[str, dict[str, tuple[int, int, int]]] = {}

    total_providers = len(providers)
    total_datasets = len(datasets)
    print(f"\nBenchmarking {total_providers} provider(s) × {total_datasets} dataset(s)")
    print(f"Providers: {', '.join(providers)}")
    print(f"Datasets:  {', '.join(datasets)}")
    print()

    try:
        for p_idx, provider_name in enumerate(providers, 1):
            results[provider_name] = {}
            print(f"[{p_idx}/{total_providers}] {provider_name}")

            for ds_idx, ds_name in enumerate(datasets, 1):
                print(f"  [{ds_idx}/{total_datasets}] {ds_name} ...", end="", flush=True)
                try:
                    summary = await run_eval(
                        provider_name=provider_name,
                        dataset_name=ds_name,
                        trigger=EvalTrigger.MANUAL,
                        config=config,
                        db=db,
                    )
                    attempted = summary.passed_cases + summary.failed_cases
                    results[provider_name][ds_name] = (
                        summary.passed_cases, attempted, summary.skipped_cases,
                    )
                    pct = (summary.passed_cases / attempted * 100) if attempted > 0 else 0
                    print(f" {summary.passed_cases}/{attempted} ({pct:.0f}%) "
                          f"[{summary.skipped_cases} skipped]  {summary.duration_s:.0f}s")
                except Exception as exc:
                    print(f" ERROR: {exc}")
                    results[provider_name][ds_name] = (0, 0, 0)

            print()

    finally:
        if db is not None:
            await db.close()

    _print_benchmark_table(providers, datasets, results)
    return 0


async def _cmd_results(args: argparse.Namespace) -> int:
    try:
        import aiosqlite  # noqa: I001

        from genesis.env import genesis_db_path
        from genesis.eval.db import get_runs

        async with aiosqlite.connect(str(genesis_db_path())) as db:
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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
        passed = run.get("passed_cases", 0)
        failed = run.get("failed_cases", 0)
        attempted = passed + failed
        pct = (passed / attempted * 100) if attempted > 0 else 0
        skipped = run.get("skipped_cases", 0)
        skipped_str = f" ({skipped} skipped)" if skipped else ""
        print(
            f"  {run.get('created_at', '?')[:19]}  "
            f"{run.get('model_id', '?'):30s}  "
            f"{run.get('dataset', '?'):20s}  "
            f"{passed}/{attempted} ({pct:.0f}%){skipped_str}  "
            f"{run.get('duration_s', 0):.1f}s"
        )
    return 0


async def _cmd_compare(args: argparse.Namespace) -> int:
    """Read latest results from DB and print comparison table."""
    try:
        import aiosqlite  # noqa: I001
        from genesis.env import genesis_db_path
        from genesis.eval.db import get_runs

        datasets = list_datasets()
        providers_seen: set[str] = set()
        results: dict[str, dict[str, tuple[int, int, int]]] = {}

        async with aiosqlite.connect(str(genesis_db_path())) as db:
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            for ds_name in datasets:
                # Fetch enough rows so each provider can contribute up to args.last runs.
                # We don't know the provider count upfront, so over-fetch generously.
                runs = await get_runs(db, dataset=ds_name, limit=500)
                # Count runs per model_id, take the most recent args.last per provider
                run_counts: dict[str, int] = {}
                for run in runs:
                    model_id = run.get("model_id", "")
                    count = run_counts.get(model_id, 0)
                    if count >= args.last:
                        continue
                    run_counts[model_id] = count + 1
                    providers_seen.add(model_id)
                    if model_id not in results:
                        results[model_id] = {}
                    # Use the most recent run (first one seen for this provider)
                    if ds_name not in results[model_id]:
                        passed = run.get("passed_cases", 0)
                        failed = run.get("failed_cases", 0)
                        skipped = run.get("skipped_cases", 0)
                        results[model_id][ds_name] = (passed, passed + failed, skipped)

    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not results:
        print("no eval runs found in DB. Run `genesis eval benchmark` first.")
        return 0

    providers = sorted(providers_seen)
    _print_benchmark_table(providers, datasets, results)
    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    """Export benchmark results as a markdown document."""
    try:
        import aiosqlite  # noqa: I001

        from genesis.env import genesis_db_path
        from genesis.eval.db import get_runs
        from genesis.routing.config import load_config

        config_path = __import__("pathlib").Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"
        config = load_config(config_path)

        datasets = list_datasets()
        results: dict[str, dict[str, tuple[int, int, int]]] = {}
        provider_notes: dict[str, str] = {}

        async with aiosqlite.connect(str(genesis_db_path())) as db:
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            for ds_name in datasets:
                runs = await get_runs(db, dataset=ds_name, limit=500)
                seen: set[str] = set()
                for run in runs:
                    model_id = run.get("model_id", "")
                    if model_id in seen:
                        continue
                    seen.add(model_id)
                    if model_id not in results:
                        results[model_id] = {}
                    passed = run.get("passed_cases", 0)
                    failed = run.get("failed_cases", 0)
                    skipped = run.get("skipped_cases", 0)
                    results[model_id][ds_name] = (passed, passed + failed, skipped)

    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not results:
        print("no eval runs found in DB. Run `genesis eval benchmark` first.", file=sys.stderr)
        return 1

    # Determine free/paid status from config
    for provider_name in results:
        cfg = config.providers.get(provider_name)
        if cfg:
            provider_notes[provider_name] = "free" if cfg.is_free else "paid"

    md = _generate_export_markdown(datasets, results, provider_notes)

    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(f"exported to {args.output}")
    else:
        print(md)

    return 0


def _generate_export_markdown(
    datasets: list[str],
    results: dict[str, dict[str, tuple[int, int, int]]],
    provider_notes: dict[str, str],
) -> str:
    """Generate benchmark results markdown from DB data."""
    from datetime import UTC, datetime

    lines = []
    lines.append("# Model Benchmark Results\n")
    lines.append(f"Last updated: {datetime.now(UTC).strftime('%Y-%m-%d')}\n")
    lines.append(
        "Benchmark methodology: 3 datasets (classification, extraction, structured_output)"
    )
    lines.append(
        "with 37 total cases. Scores are `passed/attempted` — skipped cases (rate limits,"
    )
    lines.append(
        "API errors) excluded from the denominator. All runs use the eval harness in"
    )
    lines.append("`src/genesis/eval/` with binary pass/fail scorers (no LLM-as-judge).\n")
    lines.append("## Current Results\n")
    lines.append("Best run per provider. Providers marked (free) cost $0; others are paid.\n")

    # Build table header
    header = "| Provider |"
    sep = "|---|"
    for ds in datasets:
        header += f" {ds.replace('_', ' ').title()} |"
        sep += "---|"
    header += " AVG | Notes |"
    sep += "---|---|"
    lines.append(header)
    lines.append(sep)

    # Compute averages and sort
    provider_avgs: list[tuple[str, float]] = []
    for provider in results:
        pcts = []
        for ds in datasets:
            passed, attempted, _sk = results[provider].get(ds, (0, 0, 0))
            if attempted > 0:
                pcts.append(passed / attempted * 100)
        avg = sum(pcts) / len(pcts) if pcts else 0
        provider_avgs.append((provider, avg))

    provider_avgs.sort(key=lambda x: -x[1])

    for provider, avg in provider_avgs:
        row = f"| {provider} |"
        for ds in datasets:
            passed, attempted, skipped = results[provider].get(ds, (0, 0, 0))
            if attempted == 0 and skipped == 0:
                row += " n/a |"
            elif attempted == 0:
                row += f" -/{skipped}sk |"
            else:
                pct = passed / attempted * 100
                cell = f"{passed}/{attempted} ({pct:.0f}%)"
                if skipped:
                    cell += f" +{skipped}sk"
                row += f" {cell} |"
        note = provider_notes.get(provider, "")
        row += f" **{avg:.0f}%** | {note.title()} |"
        lines.append(row)

    lines.append("")
    lines.append("## Scoring Methodology\n")
    lines.append("- **Fair denominator**: `passed / (passed + failed)`. Skipped cases excluded.")
    lines.append("- **Binary scoring**: Pass or fail, no partial credit.")
    lines.append("- **Rate-aware**: Each provider throttled to its `rpm_limit`.")
    lines.append("- **Retry**: 2 retries with exponential backoff on transient errors.\n")
    lines.append("## Run History\n")
    lines.append("Stored in `eval_runs` and `eval_results` tables in `genesis.db`.")
    lines.append("Query with `genesis eval results` or `genesis eval compare`.\n")

    return "\n".join(lines)


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
    attempted = summary.passed_cases + summary.failed_cases
    pct = (summary.passed_cases / attempted * 100) if attempted > 0 else 0
    print(f"  Run ID:   {summary.run_id[:12]}")
    print(f"  Model:    {summary.model_id} ({summary.model_profile})")
    print(f"  Dataset:  {summary.dataset}")
    print(f"  Results:  {summary.passed_cases}/{attempted} passed ({pct:.0f}%)", end="")
    if summary.skipped_cases:
        print(f"  [{summary.skipped_cases} skipped — excluded from score]", end="")
    print()
    print(f"  Duration: {summary.duration_s:.1f}s")
    print()

    # Per-case breakdown
    for r in summary.results:
        if r.skipped:
            status = "SKIP"
        elif r.passed:
            status = "PASS"
        else:
            status = "FAIL"
        print(f"    [{status}] {r.case_id}", end="")
        if r.scorer_detail:
            print(f"  -- {r.scorer_detail}", end="")
        if r.latency_ms > 0:
            print(f"  ({r.latency_ms:.0f}ms)", end="")
        print()


def _print_benchmark_table(
    providers: list[str],
    datasets: list[str],
    results: dict[str, dict[str, tuple[int, int, int]]],
) -> None:
    """Print a formatted comparison table of benchmark results.

    results[provider][dataset] = (passed, attempted, skipped)
    """
    if not providers or not datasets:
        return

    # Column widths
    name_w = max(len(p) for p in providers) + 2
    name_w = max(name_w, 28)
    ds_w = max(max(len(d) for d in datasets), 14)
    col_w = ds_w + 2

    print("\n" + "=" * (name_w + col_w * len(datasets) + 10))
    print("  BENCHMARK RESULTS  (score = passed/attempted, skipped excluded)")
    print("=" * (name_w + col_w * len(datasets) + 10))

    # Header row
    header = f"  {'Provider':<{name_w}}"
    for ds in datasets:
        header += f"  {ds:^{col_w}}"
    header += f"  {'AVG':^8}"
    print(header)
    print("  " + "-" * (name_w + col_w * len(datasets) + 8))

    # Data rows
    for provider in providers:
        row = f"  {provider:<{name_w}}"
        ds_pcts: list[float] = []
        for ds in datasets:
            passed, attempted, skipped = results.get(provider, {}).get(ds, (0, 0, 0))
            if attempted == 0 and skipped == 0:
                cell = "  n/a"
            elif attempted == 0:
                cell = f"  -/{skipped}sk"
            else:
                pct = passed / attempted * 100
                ds_pcts.append(pct)
                cell = f"{passed}/{attempted} ({pct:.0f}%)"
                if skipped:
                    cell += f"+{skipped}sk"
            row += f"  {cell:^{col_w}}"
        avg = sum(ds_pcts) / len(ds_pcts) if ds_pcts else 0.0
        row += f"  {avg:>6.0f}%"
        print(row)

    print("=" * (name_w + col_w * len(datasets) + 10))
    print()
