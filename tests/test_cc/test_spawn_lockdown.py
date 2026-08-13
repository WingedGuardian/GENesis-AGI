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
NOTE: the inbox/mail judges are spawn-hardened here but are NOT yet fully read-only —
they still leave ``Bash`` (and, for inbox, memory/settings MCP writes) available on
external input; that broader boundary is a separate, tracked follow-up, not this guard's
concern. Deliberately NOT covered here at all:
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
    from genesis.inbox.monitor import _EVAL_DISALLOWED_TOOLS

    assert set(_EVAL_DISALLOWED_TOOLS) >= _SPAWN


def test_mail_judge_denies_spawn():
    from genesis.mail.monitor import _JUDGE_DISALLOWED_TOOLS

    assert set(_JUDGE_DISALLOWED_TOOLS) >= _SPAWN


def test_experimentation_completion_denies_spawn():
    from genesis.experimentation.cc_router import _CLI_DISALLOWED_TOOLS

    assert set(_CLI_DISALLOWED_TOOLS) >= _SPAWN
