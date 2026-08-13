"""Guardrail: every read-only restricted CC session denies the whole spawn class.

A read-only reasoning session escapes its lockdown if it can spawn a child that runs
with a fresh, unrestricted toolset (Bash/Write/Edit). The spawn/escape class is
Agent (current subagent tool) + Task (obsolete alias) + Workflow (orchestrates
subagents) + Skill (some run in a subagent) — the full set in ``SPAWN_TOOL_NAMES``.
The denylists had DRIFTED (reflection blocked all four; surplus/inbox/mail/
experimentation blocked only a subset — or none). This test locks the class to a
single source of truth so a new read-only denylist that forgets it fails CI.

Scope: the strictly READ-ONLY reasoning sessions where spawning is always an escape —
reflection, surplus, the inbox/mail judges, the experimentation single-turn completion,
and sentinel-degraded. Deliberately NOT covered here (they legitimately spawn/orchestrate
or hold broader tools, and need a separate design — tracked follow-up):
``cc/direct_session`` (its ``research`` profile runs a documented deep-research
``Workflow``), ``autonomy/executor/research.py`` + ``step_dispatcher``, and the eval
bench (full builtins by design, for fairness).
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

    disallowed = set(SessionConfigBuilder().build_reflection_disallowed())
    assert disallowed >= _SPAWN, f"reflection must deny {_SPAWN - disallowed}"


def test_surplus_readonly_denies_spawn():
    from genesis.cc.session_config import _READONLY_DISALLOWED

    assert set(_READONLY_DISALLOWED) >= _SPAWN


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
