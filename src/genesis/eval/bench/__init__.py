"""Genesis-vs-bare-Claude A/B bench harness (WS-1 A3).

``python -m genesis eval bench`` runs a fixed task set through two arms —
a cognition-enabled Genesis session (identity + read-only memory recall
against an isolated DB snapshot) and a bare Claude Code session (no Genesis
context at all) — scores both with a versioned LLM-judge rubric, and persists
the paired comparison to ``eval_runs``/``eval_results``.

Package map:
  - ``types``      — BenchTask / BenchArmOutcome / BenchPair / BenchReport
  - ``tasks``      — private-JSONL task loader (refuses in-repo task files)
  - ``arms``       — the two CCInvocation builders + memory-tool policy
  - ``isolation``  — DB snapshot, bench MCP config, prod-delta probes
  - ``runner``     — paired orchestration, judging, persistence, stats
  - ``report``     — console + JSON report (always stamps judge_calibrated)

The REAL task set lives outside the repo (default
``~/.genesis/eval/bench_tasks_v1.jsonl``) because tasks derive from private
history; the repo ships only synthetic exemplars for tests. See the plan
records in the A3 section of the WS-1 plan for the isolation proofs.
"""

from genesis.eval.bench.types import (  # noqa: F401
    BenchArmOutcome,
    BenchPair,
    BenchReport,
    BenchTask,
)
