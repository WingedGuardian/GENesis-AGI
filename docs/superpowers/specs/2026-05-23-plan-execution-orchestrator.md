# Plan Execution Orchestrator — Design Spec

**Status:** Proposed (Phase 2)
**Author:** Genesis + User
**Date:** 2026-05-23
**Depends on:** feat/deterministic-steps (deterministic step executor)

## Problem

After ExitPlanMode, the current workflow relies on text-based reminders
(via PostToolUse hook) and skill instructions to guide the AI through a
structured execution process.  This is advisory — not enforced.  The AI
may skip checkpoints, forget to lint, or not commit between tasks.

Archon solves this with a YAML workflow DAG where deterministic nodes
(bash, git) are enforced structurally, not behaviorally.

## Proposal

A Genesis-native plan runner that provides MCP tools for foreground CC
sessions to call during plan execution.  The runner reads the plan file,
tracks progress, and enforces deterministic checkpoints.

### MCP Tools

```
plan_start(plan_path: str) -> PlanState
    Parse plan file, extract tasks/steps, initialize execution state.
    Returns: task list with step counts, complexity classification.

plan_checkpoint(task_idx: int) -> CheckpointResult
    Run deterministic checks for the just-completed task:
    - ruff check on modified files (via git diff --name-only)
    - pytest on relevant test files (convention-based discovery)
    - git status check (no uncommitted changes)
    Returns: pass/fail with specific failures listed.
    Blocks: if checkpoint fails, the tool returns the failure and
    expects the caller to fix before proceeding.

plan_status() -> PlanState
    Current progress: which tasks are done, which are pending.

plan_complete() -> CompletionSummary
    Finalize the plan: run full lint + test suite, summarize changes,
    present finishing options (PR, merge, iterate).
```

### Execution Model

The foreground CC session drives the plan, calling MCP tools at each
transition point.  The orchestrator enforces checkpoints — the CC
session cannot skip them because the tool call gates progress.

```
CC session                    Plan Runner (MCP)
-----------                   -----------------
plan_start(plan_path)  ────►  Parse plan, initialize state
                       ◄────  Task list + classification

[implement task 0]

plan_checkpoint(0)     ────►  Run lint + test + git status
                       ◄────  PASS or FAIL with details

[fix if failed, then...]

plan_checkpoint(0)     ────►  Re-run checks
                       ◄────  PASS

[implement task 1]

plan_checkpoint(1)     ────►  Run lint + test + git status
                       ◄────  PASS

...

plan_complete()        ────►  Full suite + summary
                       ◄────  Completion report
```

### Shared Infrastructure

The plan runner reuses `deterministic.py` from the task executor for
subprocess execution.  The same safety guardrails and output formatting
apply.

### Relationship to Task Executor

The plan runner is for **foreground interactive sessions** — the user is
present, the CC session is driving.  The task executor is for **autonomous
background execution** — Genesis dispatches steps without the user.

They share:
- `deterministic.py` — subprocess execution with guardrails
- `StepType` enum — bash/test/git types
- Checkpoint patterns — lint + test after each task

They differ in:
- **Orchestration**: plan runner = CC session calls MCP tools.
  Task executor = engine.py state machine drives CC sessions.
- **Approval**: plan runner = user approved the plan in foreground.
  Task executor = approval gates per step type.
- **Recovery**: plan runner = user fixes failures interactively.
  Task executor = 4-layer automated recovery.

### Relationship to Archon

Archon's YAML DAG model is the inspiration.  Key differences:

- Archon defines workflows as YAML files.  Genesis's plan runner reads
  markdown plan files (existing convention).
- Archon's nodes are typed (bash, git, ai).  Genesis has `StepType` enum.
- Archon manages agent lifecycle.  Genesis delegates to CC sessions.
- Archon has explicit fork/join parallelism.  Genesis runs tasks
  sequentially (parallelism is a future enhancement).

### State Storage

Plan execution state is ephemeral (in-memory dict keyed by plan_path).
It does not persist across server restarts — the plan file itself is the
source of truth (checkbox markers `- [x]` track completion).

## Non-Goals

- **DAG dependency enforcement** — tasks execute sequentially for now.
- **Parallel fan-out** — one task at a time in foreground sessions.
- **Persistent workflow definitions** — plans are markdown, not YAML DAGs.

## Implementation Estimate

- MCP tool registration: `src/genesis/mcp/health/plan_tools.py` (new file)
- Plan parser: `src/genesis/plan/parser.py` (extract tasks from markdown)
- Checkpoint runner: reuses `deterministic.py`
- Test file discovery: convention-based (`tests/test_<module>/test_<file>.py`)
