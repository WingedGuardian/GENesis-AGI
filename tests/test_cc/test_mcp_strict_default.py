"""Secure-by-default MCP scoping for autonomous CC sessions (follow-up a8f15c94).

Claude Code's ``--mcp-config`` is ADDITIVE: without ``--strict-mcp-config`` a session
also loads the user-scoped ``~/.claude.json`` MCP servers (gitnexus, codebase-memory,
serena at repo cwd) — arbitrary-source-edit / graph-mutation tools that bypass the
Edit/Write PreToolUse hooks. The fix flips ``CCInvocation.strict_mcp_config`` to default
True (secure-by-default); human-driven foreground/interactive sites opt out. These tests
pin that contract and guard the regressions that keep reopening (a new autonomous site
forgetting strict; a reflection arm bypassing the shared lockdown).

Empirical basis (probe-verified 2026-08-09, CC 2.1.x): strict + genesis config → only
those servers; strict + no config → zero servers, exit 0; ``--bare`` + strict → exit 1.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

import genesis.cc.direct_session as direct_session
from genesis.cc.direct_session import (
    _PROFILE_TO_MCP,
    _UNIVERSAL_DISALLOW,
    DirectSessionRequest,
    DirectSessionRunner,
)
from genesis.cc.session_config import (
    _USER_SCOPED_MCP_WILDCARDS,
    SessionConfigBuilder,
)
from genesis.cc.types import CCInvocation

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "genesis"


def test_ccinvocation_strict_mcp_config_default_true():
    """The flip: a plain invocation is strict by default (fail-closed to no leak)."""
    assert CCInvocation(prompt="x").strict_mcp_config is True


def test_user_scoped_wildcards_nonempty_and_shared_no_drift():
    """The user-scoped denies are one shared constant, present in BOTH the DirectSession
    universal denylist and the reflection denylist — so the two can't drift apart."""
    assert _USER_SCOPED_MCP_WILDCARDS  # non-empty
    assert set(_USER_SCOPED_MCP_WILDCARDS) <= set(_UNIVERSAL_DISALLOW)
    refl = set(SessionConfigBuilder().build_reflection_disallowed())
    assert set(_USER_SCOPED_MCP_WILDCARDS) <= refl
    # Sanity: the three known user-scoped servers are covered by wildcard.
    assert {"mcp__serena__*", "mcp__gitnexus__*", "mcp__codebase-memory-mcp__*"} <= set(
        _USER_SCOPED_MCP_WILDCARDS
    )


def test_reflection_lockdown_kwargs_is_read_only():
    """The shared reflection helper yields strict + a real config + a denylist that
    denies writes/user-scoped servers but keeps observation_write."""
    import genesis.cc.reflection_bridge._bridge as bridge

    lk = bridge._reflection_lockdown_kwargs("reflection")
    assert lk["strict_mcp_config"] is True
    assert lk["mcp_config"]  # never None (avoids the bare-strict combo)
    denied = set(lk["disallowed_tools"])
    assert {"mcp__serena__*", "mcp__gitnexus__*", "mcp__codebase-memory-mcp__*"} <= denied
    assert "mcp__genesis-health__follow_up_create" in denied
    assert "Bash" in denied and "Write" in denied and "Task" in denied
    assert "mcp__genesis-memory__observation_write" not in denied


def test_reflection_lockdown_none_profile_points_at_no_mcp():
    """LIGHT / fail-closed profile still supplies a real empty config (never None)."""
    import genesis.cc.reflection_bridge._bridge as bridge

    lk = bridge._reflection_lockdown_kwargs("none")
    assert str(lk["mcp_config"]).endswith("no_mcp.json")
    assert lk["strict_mcp_config"] is True


def _functions_containing_ccinvocation(path: pathlib.Path) -> dict[str, str]:
    """Map function name -> source, for every function whose body constructs a
    CCInvocation."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "CCInvocation"
            ):
                out[node.name] = ast.get_source_segment(src, node) or ""
                break
    return out


def test_all_reflection_arms_use_the_lockdown_helper():
    """Every reflection entry point that builds a CCInvocation MUST route through
    _reflection_lockdown_kwargs — this is the guard that keeps a NEW arm (like the
    weekly-assessment / quality-calibration gap in PR #1343) from silently running
    unrestricted."""
    bridge = _SRC / "cc" / "reflection_bridge" / "_bridge.py"
    funcs = _functions_containing_ccinvocation(bridge)
    assert funcs, "expected reflection CCInvocation construction sites"
    offenders = [name for name, body in funcs.items() if "_reflection_lockdown_kwargs" not in body]
    assert not offenders, (
        "reflection functions build a CCInvocation without the shared read-only "
        f"lockdown helper (they would run unrestricted): {offenders}"
    )


def test_step_dispatcher_code_denylist_recipe_denies_admin_keeps_reads_and_builtins():
    """The exact denylist recipe step_dispatcher applies to CODE/VERIFICATION steps —
    the MCP portion of build_reflection_disallowed — must DENY the write/admin genesis
    MCP tools (a code step must not be able to disable the autonomous approval gate via
    settings_update, or call ego_directive/campaign_*/memory_store) and the user-scoped
    servers, while KEEPING the genesis READ tools and the built-ins (Bash/Write/Edit)
    that code steps need. This is the behavioral guard behind the source-wiring check."""
    recipe = [
        t for t in SessionConfigBuilder().build_reflection_disallowed() if t.startswith("mcp__")
    ]
    must_deny = {
        "mcp__genesis-health__settings_update",
        "mcp__genesis-health__ego_directive",
        "mcp__genesis-health__campaign_create",
        "mcp__genesis-health__module_call",
        "mcp__genesis-health__direct_session_run",
        "mcp__genesis-health__task_submit",
        "mcp__genesis-memory__memory_store",
        "mcp__genesis-memory__knowledge_ingest",
        "mcp__serena__*",
        "mcp__gitnexus__*",
        "mcp__codebase-memory-mcp__*",
    }
    assert must_deny <= set(recipe), f"recipe fails to deny: {must_deny - set(recipe)}"
    # Genesis reads stay available (a code step may recall context).
    for read_tool in (
        "mcp__genesis-health__health_status",
        "mcp__genesis-memory__memory_recall",
    ):
        assert read_tool not in recipe
    # Built-ins are NOT in the mcp-only recipe → code steps keep Bash/Write/Edit/Task.
    for builtin in ("Bash", "Write", "Edit", "Task"):
        assert builtin not in recipe


def test_step_dispatcher_wires_mcp_config_and_denylist_together():
    """Source guard: CODE/VERIFICATION steps must pass BOTH a genesis mcp_config AND a
    disallowed_tools denylist (mcp_config without a denylist was the review BLOCKER —
    it exposed the full genesis-health admin surface). Gated on is_code_or_verify."""
    src = (_SRC / "autonomy" / "executor" / "step_dispatcher.py").read_text(encoding="utf-8")
    assert "is_code_or_verify" in src
    assert "step_mcp_config = " in src and "build_mcp_config" in src
    assert "step_disallowed = " in src and "build_reflection_disallowed" in src
    assert 'startswith("mcp__")' in src  # MCP-only filter (keeps built-ins)
    assert "mcp_config=step_mcp_config" in src
    assert "disallowed_tools=step_disallowed" in src


def _count_ccinvocations_with_false_strict(path: pathlib.Path) -> int:
    """Count CCInvocation(...) calls that pass strict_mcp_config=False (AST, so an
    unrelated assignment elsewhere in the file can't satisfy it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    n = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CCInvocation"
        ):
            for kw in node.keywords:
                if (
                    kw.arg == "strict_mcp_config"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    n += 1
    return n


def test_foreground_interactive_sites_opt_out_of_strict():
    """Owner-attended interactive paths keep the full user-scoped toolset — the three
    conversation CCInvocation sites + the checkpoint resume MUST set
    strict_mcp_config=False so the secure-by-default flip doesn't silently strip the
    user's tools mid-conversation. AST-based so a stray literal can't satisfy it."""
    assert _count_ccinvocations_with_false_strict(_SRC / "cc" / "conversation.py") >= 3
    assert _count_ccinvocations_with_false_strict(_SRC / "cc" / "checkpoint.py") >= 1


def test_directsession_builtin_profiles_never_map_to_full():
    """No BUILT-IN DirectSession profile maps to the "full" MCP profile — every built-in
    stays strict-scoped. (A built-in silently going "full" would reopen the leak for that
    profile; only deliberate, trusted install-local overlays may choose "full".)"""
    assert "full" not in set(_PROFILE_TO_MCP.values())


def test_directsession_explicit_full_profile_opts_out_of_strict():
    """Codex P2 regression guard: build_mcp_config("full") returns None (CC uses its full
    default), so under the strict-by-default flip a profile that INTENDS full MCP would
    silently get ZERO servers. _build_invocation must honor an explicit mcp_profile="full"
    by opting that dispatch out of strict (full means full), while a concrete profile stays
    strict. Behavioral: exercises the real _build_invocation with a real config builder."""
    runner = DirectSessionRunner(
        invoker=MagicMock(),
        session_manager=MagicMock(),
        config_builder=SessionConfigBuilder(),
        runtime=MagicMock(),
    )
    # Concrete profile (observe -> reflection) stays strict with a real config.
    inv_norm = runner._build_invocation(
        DirectSessionRequest(profile="observe", prompt="hi"), "s-norm"
    )
    assert inv_norm.strict_mcp_config is True
    assert inv_norm.mcp_config  # a real genesis-only config path

    # Remap a valid profile to the "full" MCP profile (simulating a trusted overlay);
    # the dispatch must opt OUT of strict so CC's full default config is honored.
    orig = _PROFILE_TO_MCP["observe"]
    try:
        direct_session._PROFILE_TO_MCP["observe"] = "full"
        inv_full = runner._build_invocation(
            DirectSessionRequest(profile="observe", prompt="hi"), "s-full"
        )
    finally:
        direct_session._PROFILE_TO_MCP["observe"] = orig
    assert inv_full.strict_mcp_config is False
    assert inv_full.mcp_config is None
