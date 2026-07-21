"""The two skill-replay arms — OLD vs NEW SKILL.md content, file-free pinning.

Both arms use the eval bench's BARE-arm recipe (``safe_mode`` + strict empty MCP
+ neutral per-arm cwd + cleanroom ``CLAUDE_CONFIG_DIR``) — the strongest
customization suppression available. ``safe_mode`` is the only OAuth-compatible
way to suppress CC's native, passwd-home skill discovery (``CLAUDE_CONFIG_DIR``
does NOT relocate it), so the live on-disk SKILL.md cannot leak in alongside the
pinned copy. The ONLY delta between the two arms is the pinned skill content in
the system prompt: control = OLD (current), treatment = NEW (proposed).

``--system-prompt`` is honored under ``--safe-mode`` (probe-verified 2026-07-20:
a pinned system prompt was obeyed with safe-mode active).
"""

from __future__ import annotations

from pathlib import Path

from genesis.cc.types import CCInvocation, CCModel, EffortLevel
from genesis.env import repo_root
from genesis.eval.bench.arms import TASK_ENVELOPE
from genesis.eval.bench.types import BenchTask
from genesis.eval.skill_replay.types import ARM_NEW, ARM_OLD


def _task_workdir(run_dir: Path, task: BenchTask, arm_label: str) -> Path:
    """Neutral, empty, per-task-per-arm cwd — outside any git repo, so no project
    CLAUDE.md or ``.claude/skills`` directory is discoverable from it."""
    workdir = run_dir / "work" / task.id / arm_label
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def build_skill_arm_invocation(
    task: BenchTask,
    run_dir: Path,
    model: CCModel,
    effort: EffortLevel,
    *,
    skill_name: str,
    skill_content: str,
    bare_config_dir: Path,
    run_id: str,
    arm_label: str,
) -> CCInvocation:
    """Build one replay arm: bare-Claude isolation + the skill pinned in the
    system prompt.

    ``arm_label`` is :data:`ARM_OLD` (control, current/pre-edit content) or
    :data:`ARM_NEW` (treatment, proposed content). The system prompt mirrors how
    Genesis injects a skill in production (``direct_session`` concatenates
    ``"## Skill: <name>\\n<body>"`` into the system prompt), so the arm reads the
    pinned body exactly as a live session would — and nothing else.
    """
    if arm_label not in (ARM_OLD, ARM_NEW):
        raise ValueError(f"arm_label must be {ARM_OLD!r} or {ARM_NEW!r}, got {arm_label!r}")
    system_prompt = f"## Skill: {skill_name}\n{skill_content}"
    return CCInvocation(
        prompt=task.rendered_prompt() + TASK_ENVELOPE,
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        working_dir=str(_task_workdir(run_dir, task, arm_label)),
        timeout_s=task.timeout_s,
        skip_permissions=True,
        mcp_config=str(repo_root().resolve() / "config" / "no_mcp.json"),
        strict_mcp_config=True,
        safe_mode=True,
        claude_code_tmpdir=str(run_dir / "cc-sandbox"),
        env_overrides={"CLAUDE_CONFIG_DIR": str(bare_config_dir)},
        session_key=f"skill_replay:{run_id}:{task.id}:{arm_label}",
    )
