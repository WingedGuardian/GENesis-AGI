"""Guardrail: every restricted CC session denies the subagent-spawn tool.

A locked-down session (reflection, surplus, the direct-session profiles, the
inbox/mail judges, the experimentation completion, sentinel-degraded) MUST deny
the CC subagent-spawn tool. The live spawn tool is ``Agent`` (``Task`` is the
obsolete name); a session that can spawn a subagent escapes its lockdown, because
the subagent inherits a fresh, unrestricted toolset (Bash/Write/Edit).

The spawn/escape class is Agent (current) + Task (obsolete) + Workflow (orchestrates
subagents) + Skill (some run in a subagent) — the full set in ``SPAWN_TOOL_NAMES``.
The denylists had DRIFTED (reflection blocked all four, but surplus/direct-session/
inbox/mail/experimentation blocked only a subset — or none). This test locks the class
to a single source of truth so a new denylist that forgets it fails CI.

Scope: the READ-ONLY / restricted-lockdown sessions (reflection, surplus, the
direct-session profiles, the inbox/mail judges, the experimentation completion,
sentinel-degraded). The autonomy-executor sessions (``autonomy/executor/research.py``,
``step_dispatcher``) legitimately hold broader tools and are evaluated separately
(tracked follow-up); the eval bench intentionally runs full builtins (fairness). Those
are deliberately NOT asserted here.
"""

from __future__ import annotations

from genesis.cc.types import SPAWN_TOOL_NAMES

_SPAWN = set(SPAWN_TOOL_NAMES)


def test_spawn_tool_names_pins_the_whole_spawn_class():
    # The full spawn/escape class: Agent (current), Task (obsolete alias), Workflow
    # (orchestrates subagents), Skill (some run in a subagent). Missing any one leaves
    # an alternate escape open in every site that relies on the shared set.
    assert "Agent" in SPAWN_TOOL_NAMES
    assert SPAWN_TOOL_NAMES == ("Agent", "Task", "Workflow", "Skill")


def test_reflection_denies_spawn():
    from genesis.cc.session_config import SessionConfigBuilder

    disallowed = set(SessionConfigBuilder().build_reflection_disallowed())
    assert disallowed >= _SPAWN, f"reflection must deny {_SPAWN - disallowed}"


def test_surplus_readonly_denies_spawn():
    from genesis.cc.session_config import _READONLY_DISALLOWED

    assert set(_READONLY_DISALLOWED) >= _SPAWN


def test_every_direct_session_profile_denies_spawn():
    from genesis.cc.direct_session import PROFILES

    assert PROFILES, "no profiles registered"
    for profile, disallowed in PROFILES.items():
        missing = _SPAWN - set(disallowed)
        assert not missing, f"direct-session profile {profile!r} does not deny {missing}"


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


def test_tool_exceptions_cannot_reenable_spawn():
    """A per-request tool_exception must NOT re-open subagent spawn.

    direct_session already discards the recursive-genesis-spawn tool; this pins that
    the CC builtin Agent/Task are discarded too, so a background session can never
    grant itself spawn via tool_exceptions and escape its profile lockdown. The guard
    is targeted (a NON-spawn exception is still honored — Write below).
    """
    from unittest.mock import MagicMock

    from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner

    config_builder = MagicMock()
    config_builder.build_mcp_config.return_value = None
    runner = DirectSessionRunner(
        invoker=MagicMock(),
        session_manager=MagicMock(),
        config_builder=config_builder,
        runtime=MagicMock(),
    )
    runner._protected_paths = None
    req = DirectSessionRequest(
        prompt="x",
        profile="observe",  # observe blocks Write (via _NO_FILE_WRITE) AND spawn
        system_prompt="x",  # skip the surplus-config path
        tool_exceptions=("Agent", "Task", "Write"),
    )
    inv = runner._build_invocation(req, "sess-test")

    assert "Agent" in inv.disallowed_tools, "tool_exceptions re-enabled Agent spawn"
    assert "Task" in inv.disallowed_tools, "tool_exceptions re-enabled Task spawn"
    # Sanity: the guard is targeted — a legitimate non-spawn exception still applies.
    assert "Write" not in inv.disallowed_tools


def test_overlay_profile_is_spawn_locked_unconditionally():
    """add_profile must inject spawn-deny even if the overlay omits it."""
    from genesis.cc import direct_session as ds

    name = "_test_overlay_no_spawn_deny"
    # add_profile lives on ProfileOverlayContext and mutates the module PROFILES dict;
    # its body doesn't touch self, so a bare instance is enough to exercise it.
    ctx = ds.ProfileOverlayContext.__new__(ds.ProfileOverlayContext)
    try:
        # Overlay hands a disallow list WITHOUT any spawn tool.
        ctx.add_profile(name, disallow=["Bash"], addendum="test")
        assert set(ds.PROFILES[name]) >= _SPAWN
    finally:
        ds.PROFILES.pop(name, None)
        ds._PROFILE_ADDENDA.pop(name, None)
        ds._PROFILE_BASH_ALLOWLIST.pop(name, None)
        ds._PROFILE_TO_MCP.pop(name, None)
        ds._PROFILE_SKILLS.pop(name, None)
