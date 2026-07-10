"""The two bench arms — CCInvocation builders and the memory-tool policy.

Fairness contract: both arms get the SAME model, effort, task prompt (with the
same envelope), tool budget (built-ins fully enabled), timeout, and sandbox.
The only deltas are the treatment under test: Genesis identity (system prompt)
and read-only memory recall (genesis-memory MCP against the run's DB snapshot).

Bare-arm recipe (every piece probe-verified 2026-07-09):
  - ``--safe-mode`` — the ONLY OAuth-compatible way to suppress the user-level
    CLAUDE.md (CC discovers it via the passwd-resolved home, ignoring $HOME and
    $CLAUDE_CONFIG_DIR; ``--bare`` refuses OAuth outright).
  - clean CLAUDE_CONFIG_DIR whose only entry is a SYMLINK to the real
    ``.credentials.json`` (defense-in-depth for settings isolation; the
    symlink lets OAuth token refresh write through — a copy would rotate the
    refresh token and break the real install).
  - neutral empty cwd per task (no project CLAUDE.md), strict empty MCP.
"""

from __future__ import annotations

import os
from pathlib import Path

from genesis.cc.types import CCInvocation, CCModel, EffortLevel
from genesis.env import repo_root
from genesis.eval.bench.types import ARM_BARE, ARM_GENESIS, BenchTask

# ── Memory-tool policy ──────────────────────────────────────────────────
# EVERY tool the genesis-memory MCP server registers must appear in exactly
# one of these two sets — enforced by a static-AST forcing-function test
# (tests/test_eval/test_bench_arms.py), so a future memory tool added without
# a conscious read/write classification fails CI.

#: Read-only recall surface the Genesis arm keeps. "Read-only" holds because
#: the recall path's own usage write-backs are suppressed via
#: GENESIS_MEMORY_WRITEBACKS_OFF in the bench MCP env (see isolation.py) —
#: without that seam, memory_recall/memory_core_facts mutate prod Qdrant.
BENCH_MEMORY_READONLY_ALLOWED: frozenset[str] = frozenset({
    "conversation_history",
    "document_query",
    "knowledge_recall",
    "knowledge_status",
    "locate",
    "memory_core_facts",
    "memory_expand",
    "memory_proactive",
    "memory_recall",
    "memory_stats",
    "observation_query",
    "procedure_recall",
    "reference_export",
    "reference_lookup",
})

#: Mutating tools, stripped from the arm. The DB snapshot would absorb the
#: SQLite ones harmlessly, but (a) several write the SHARED prod Qdrant
#: (store/synthesize/extract/ingest*/document_index), and (b) a bench arm
#: that "learns" mid-run corrupts the frozen-memory premise either way.
#: resume_review is here conservatively: it mutates review bookkeeping and
#: the arm has no use for it.
BENCH_MEMORY_WRITE_DISALLOWED: frozenset[str] = frozenset({
    "bookmark_shelve",
    "bookmark_unshelve",
    "document_delete",
    "document_index",
    "knowledge_ingest",
    "knowledge_ingest_batch",
    "knowledge_ingest_source",
    "memory_extract",
    "memory_store",
    "memory_synthesize",
    "observation_resolve",
    "observation_write",
    "procedure_store",
    "reference_delete",
    "reference_store",
    "resume_review",
})

#: The --disallowedTools list for the Genesis arm (CC tool-name format,
#: matching direct_session.py's lists).
BENCH_MEMORY_DISALLOW: list[str] = [
    f"mcp__genesis-memory__{name}" for name in sorted(BENCH_MEMORY_WRITE_DISALLOWED)
]

# ── Shared task envelope ────────────────────────────────────────────────
# Appended to the task prompt for BOTH arms identically (fairness: neither
# arm gets extra output guidance the other lacks). CCOutput's text is the
# session's final message — the judge grades exactly that.
TASK_ENVELOPE = (
    "\n\nDeliver your complete final answer in your final message — it is "
    "the only output that will be evaluated. Do not end with a question or "
    "an offer to continue."
)

#: Genesis-arm system-prompt addendum (styled after direct_session's profile
#: addenda): points the arm at its memory without scripting the answer.
#: The wait-and-retry protocol is load-bearing: the memory server takes
#: ~10-15s to import/connect, and `claude -p` does NOT block the first turn
#: on MCP startup — without the wait, the arm inventories tools at t≈2s,
#: finds nothing, and answers cold (both 2026-07-09 shakedowns failed this
#: way; the retry probe then confirmed the tools register mid-session).
_BENCH_ADDENDUM = (
    "\n\n---\n\n"
    "You have access to your memory system via the genesis-memory MCP tools. "
    "IMPORTANT: those tools can take ~20 seconds to register after session "
    "start. Before answering, confirm memory tools are available (e.g. "
    "search for memory_recall); if not yet available, run `sleep 5` in Bash "
    "and re-check — up to six times. Recall relevant memories, knowledge, "
    "and procedures before answering when the task touches prior work or "
    "stored facts. Memory is READ-ONLY in this session: store nothing, and "
    "do not attempt memory writes."
)


def scrub_nested_cc_env() -> list[str]:
    """Remove nested-CC vars from THIS process's environment.

    ``genesis eval bench`` launched from inside a CC session inherits ~7
    ``CLAUDE*`` vars (CLAUDECODE, CLAUDE_CODE_SESSION_ID, _EXECPATH, ...)
    that would leak into both arms and mark them as nested sessions
    (probe-observed 2026-07-09; CCInvoker pops only two of them). The
    invoker rebuilds everything it needs (CLAUDE_CODE_TMPDIR etc.) per
    invocation, so dropping the whole prefix here is safe. Returns the
    removed names (for logging).
    """
    removed = [k for k in os.environ if k.startswith("CLAUDE")]
    for key in removed:
        os.environ.pop(key, None)
    return removed


def prepare_bare_config_dir(run_dir: Path) -> Path:
    """Cleanroom CLAUDE_CONFIG_DIR: only a credentials symlink inside."""
    cfg = run_dir / "bare-claude-config"
    cfg.mkdir(parents=True, exist_ok=True)
    link = cfg / ".credentials.json"
    real = Path.home() / ".claude" / ".credentials.json"
    if not real.exists():
        raise RuntimeError(
            f"no CC credentials at {real} — the bare arm cannot authenticate"
        )
    if not link.is_symlink():
        link.symlink_to(real)
    return cfg


def _task_workdir(run_dir: Path, task: BenchTask, arm: str) -> Path:
    """Neutral, empty, per-task-per-arm cwd — outside any git repo."""
    workdir = run_dir / "work" / task.id / arm
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def build_bare_arm_invocation(
    task: BenchTask,
    run_dir: Path,
    model: CCModel,
    effort: EffortLevel,
    bare_config_dir: Path,
    run_id: str,
) -> CCInvocation:
    """Control arm: tool-enabled Claude Code with zero Genesis context."""
    return CCInvocation(
        prompt=task.rendered_prompt() + TASK_ENVELOPE,
        model=model,
        effort=effort,
        working_dir=str(_task_workdir(run_dir, task, ARM_BARE)),
        timeout_s=task.timeout_s,
        skip_permissions=True,
        mcp_config=str(repo_root().resolve() / "config" / "no_mcp.json"),
        strict_mcp_config=True,
        safe_mode=True,
        claude_code_tmpdir=str(run_dir / "cc-sandbox"),
        env_overrides={"CLAUDE_CONFIG_DIR": str(bare_config_dir)},
        session_key=f"bench:{run_id}:{task.id}:{ARM_BARE}",
    )


def build_genesis_arm_invocation(
    task: BenchTask,
    run_dir: Path,
    model: CCModel,
    effort: EffortLevel,
    mcp_config_path: Path,
    run_id: str,
) -> CCInvocation:
    """Treatment arm: dispatched-session shape — identity via system prompt,
    read-only memory MCP against the run's DB snapshot, neutral cwd.

    Inheriting the user-level CLAUDE.md here is production-faithful (real
    dispatched sessions get it too), so no safe_mode. GENESIS_CC_SESSION=1
    (set by the invoker) makes Genesis hooks self-skip, as in production
    background sessions.
    """
    from genesis.cc.session_config import SessionConfigBuilder

    system_prompt = SessionConfigBuilder()._load_identity_block() + _BENCH_ADDENDUM
    return CCInvocation(
        prompt=task.rendered_prompt() + TASK_ENVELOPE,
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        working_dir=str(_task_workdir(run_dir, task, ARM_GENESIS)),
        timeout_s=task.timeout_s,
        skip_permissions=True,
        mcp_config=str(mcp_config_path),
        strict_mcp_config=True,
        disallowed_tools=list(BENCH_MEMORY_DISALLOW),
        claude_code_tmpdir=str(run_dir / "cc-sandbox"),
        session_key=f"bench:{run_id}:{task.id}:{ARM_GENESIS}",
    )
