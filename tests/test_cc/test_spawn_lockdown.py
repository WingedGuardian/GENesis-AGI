"""Guardrail: restricted CC sessions deny the whole subagent-SPAWN class.

A restricted session escapes its lockdown if it can spawn a child that runs with a
fresh, unrestricted toolset. The spawn/escape class is Agent (current subagent tool) +
Task (obsolete alias) + Workflow (orchestrates subagents) + Skill (some run in a
subagent) — the full set in ``SPAWN_TOOL_NAMES``. The denylists had DRIFTED (reflection
blocked all but ``Agent`` — i.e. Task/Workflow/Skill; the inbox/mail judges + the
experimentation completion blocked only ``Agent`` or a subset). This test locks the class
to a single source of truth so a new denylist that forgets it fails CI. Each assertion
targets what the LIVE session actually feeds to ``claude -p``'s ``--disallowedTools``
(reflection via ``build_reflection_disallowed``; the inbox/mail/experimentation constants
ARE the value passed to their ``CCInvocation``), not an orphaned constant.

Scope — this guard is about the SPAWN class ONLY, across: reflection (fully read-only),
the inbox/mail judges, the experimentation single-turn completion, and sentinel-degraded.
NOTE: the mail judge is now fully denied ``Bash`` (its prompt uses no tools). The inbox
judge additionally denies every genesis MCP *write* (memory_store/settings_update/…)
while KEEPING the reads + ``observation_write`` its prompt needs; its ONLY remaining
residual is ``Bash`` (retained for the yt-dlp/curl YouTube-fetch path — relocating that
into Python is follow-up 727a3724). Deliberately NOT covered here at all:
- ``surplus`` — NOT a CC session: the live ``SurplusLLMExecutor`` runs via the tool-less
  Router (no ``claude -p``, no spawn tools to deny), so it is outside this scope.
- ``cc/direct_session`` (its ``research`` profile runs a documented deep-research
  ``Workflow``), ``autonomy/executor/research.py`` + ``step_dispatcher`` — legitimately
  spawn/orchestrate; need a separate design (tracked follow-up).
- the eval bench — full builtins by design (fairness).
"""

from __future__ import annotations

from genesis.cc.types import SPAWN_TOOL_NAMES

_SPAWN = set(SPAWN_TOOL_NAMES)


def test_spawn_tool_names_pins_the_whole_spawn_class():
    # The full spawn/escape class: Agent (current), Task (obsolete alias), Workflow
    # (orchestrates subagents), Skill (some run in a subagent). Missing any one leaves
    # an alternate escape open in every read-only site that relies on the shared set.
    assert "Agent" in SPAWN_TOOL_NAMES
    assert SPAWN_TOOL_NAMES == ("Agent", "Task", "Workflow", "Skill")


def test_reflection_denies_spawn():
    from genesis.cc.session_config import SessionConfigBuilder

    # Assert the LIVE reflection invocation denylist (build_reflection_disallowed),
    # not just a constant — this is what reflection_bridge feeds to disallowed_tools.
    disallowed = set(SessionConfigBuilder().build_reflection_disallowed())
    assert disallowed >= _SPAWN, f"reflection must deny {_SPAWN - disallowed}"


def test_sentinel_degraded_denies_spawn():
    from genesis.sentinel.dispatcher import _DEGRADED_DISALLOWED_TOOLS

    assert set(_DEGRADED_DISALLOWED_TOOLS) >= _SPAWN


def test_inbox_eval_judge_denies_spawn():
    from genesis.inbox.monitor import _eval_disallowed_tools

    assert set(_eval_disallowed_tools()) >= _SPAWN


def test_mail_judge_denies_spawn():
    from genesis.mail.monitor import _JUDGE_DISALLOWED_TOOLS

    assert set(_JUDGE_DISALLOWED_TOOLS) >= _SPAWN


def test_experimentation_completion_denies_spawn():
    from genesis.experimentation.cc_router import _CLI_DISALLOWED_TOOLS

    assert set(_CLI_DISALLOWED_TOOLS) >= _SPAWN


# ── 727a3724 (partial): judge Bash + inbox MCP-write hardening ────────────────
# The inbox/mail judges run skip_permissions=True on EXTERNAL, adversarial input.
# These lock the LOSSLESS half of that hardening: no Bash on the mail judge (its
# prompt uses no tools) and no dangerous MCP writes on the inbox judge, without
# removing any capability either prompt actually uses.


def test_mail_judge_denies_full_action_class():
    # The mail prompt (MAIL_EVALUATE.md) uses NO tools, so it runs act-nothing:
    # deny Bash + all file-edit + spawn + side-effecting actions + web (the last
    # closes the Read+WebFetch exfiltration path).
    from genesis.mail.monitor import _JUDGE_DISALLOWED_TOOLS

    d = set(_JUDGE_DISALLOWED_TOOLS)
    for t in (
        "Bash",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "CronCreate",
        "RemoteTrigger",
        "PushNotification",
        "SendMessage",
        "Monitor",
    ):
        assert t in d, f"mail judge must deny {t}"


def test_mail_judge_no_mcp_config_exists():
    # Regression for the parents[2] path bug: the mail judge's empty-MCP config
    # must resolve to a REAL file (strict_mcp_config requires a readable path;
    # the old hand-counted path pointed at a nonexistent src/config/no_mcp.json).
    from pathlib import Path

    from genesis.cc.session_config import SessionConfigBuilder

    p = SessionConfigBuilder().build_mcp_config("none")
    assert p and Path(p).exists(), f"no_mcp config must exist, got {p!r}"


def test_inbox_judge_denies_mcp_writes():
    from genesis.inbox.monitor import _eval_disallowed_tools

    d = set(_eval_disallowed_tools())
    assert "mcp__genesis-memory__memory_store" in d
    assert "mcp__genesis-health__settings_update" in d
    assert "mcp__genesis-health__follow_up_create" in d


def test_inbox_judge_keeps_reads_and_observation_write():
    # Guard against OVER-denial: the prompt needs these reads + its one sanctioned
    # write (observation_write). Denying them would silently break inbox eval.
    from genesis.inbox.monitor import _eval_disallowed_tools

    d = set(_eval_disallowed_tools())
    assert "mcp__genesis-memory__memory_recall" not in d
    assert "mcp__genesis-memory__procedure_recall" not in d
    assert "mcp__genesis-memory__observation_write" not in d


def test_inbox_judge_retains_bash_residual():
    # DELIBERATE residual (727a3724): Bash is kept so the prompt can shell out to
    # yt-dlp/curl for YouTube inbox URLs. If a future change denies Bash WITHOUT
    # relocating that fetch into Python, YouTube inbox items break — this pins the
    # residual so the trade-off is made consciously, not by accident.
    from genesis.inbox.monitor import _eval_disallowed_tools

    assert "Bash" not in _eval_disallowed_tools()
