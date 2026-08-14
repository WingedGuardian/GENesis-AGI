"""Reflection tool-scope lockdown + coverage guardrail.

Reflection sessions (deep/strategic) must be READ-ONLY + observation-writing only.
The denylist is DERIVED as (live MCP registry − read-allowlist − observation_write),
so a future write tool is auto-denied. These tests are the guardrail that keeps the
read-allowlist honest against the live registry, and they pin the composition
(known writes denied, reads + observation_write available).

Why a denylist and not --allowedTools: verified empirically 2026-08-07 that
`--allowedTools` is NOT a strict allowlist under `--dangerously-skip-permissions`
(it left Bash available); only `--disallowedTools` removes a tool.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from genesis.cc.session_config import (
    _REFLECTION_DENY_BUILTINS,
    _REFLECTION_MCP_SERVERS,
    _REFLECTION_READ_MCP,
    _REFLECTION_WRITE_MCP,
    SessionConfigBuilder,
    _registered_mcp_tool_names,
)

# Sentinel writes that MUST always be denied to reflection (fabrication / mutation
# / spawn / cost surface). Not exhaustive — the coverage test enforces the rest.
_MUST_DENY_MCP = [
    "mcp__genesis-health__follow_up_create",
    "mcp__genesis-health__follow_up_update",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__session_config",  # setter despite getter-shaped name
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__task_control",
    "mcp__genesis-health__ego_directive",
    "mcp__genesis-health__ego_goal_create",
    "mcp__genesis-health__module_call",
    "mcp__genesis-health__direct_session_run",
    "mcp__genesis-health__deliberate",  # PAID model-panel call
    "mcp__genesis-health__campaign_create",
    "mcp__genesis-health__browser_run_js",
    "mcp__genesis-memory__memory_store",
    "mcp__genesis-memory__memory_synthesize",
    "mcp__genesis-memory__knowledge_ingest",
    "mcp__genesis-memory__observation_resolve",
    "mcp__genesis-memory__reference_store",
]
_MUST_DENY_BUILTINS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Agent",
    "Task",
    "Workflow",
    "Skill",
]

# Read tools that MUST stay available (absent from the denylist).
_MUST_ALLOW = [
    "mcp__genesis-memory__observation_write",  # the ONE sanctioned write
    "mcp__genesis-memory__memory_recall",
    "mcp__genesis-memory__observation_query",
    "mcp__genesis-health__health_status",
    "mcp__genesis-health__follow_up_list",
    "mcp__genesis-memory__reference_export",
]


@pytest.fixture
def disallowed():
    return set(SessionConfigBuilder().build_reflection_disallowed())


@pytest.mark.parametrize("tool", _MUST_DENY_MCP + _MUST_DENY_BUILTINS)
def test_write_tools_are_denied(disallowed, tool):
    assert tool in disallowed, f"{tool} must be denied to reflection sessions"


@pytest.mark.parametrize("tool", _MUST_ALLOW)
def test_read_and_observation_write_are_allowed(disallowed, tool):
    assert tool not in disallowed, f"{tool} must remain available to reflection"


def test_default_deny_every_registered_tool_is_read_or_denied(disallowed):
    """Fail-closed property: every live-registered MCP tool of the reflection servers is
    EITHER explicitly read-allowed (or observation_write) OR in the derived denylist.
    Because the denylist is derived as (registry − read − observation_write), a tool not
    in the read set is ALWAYS denied — so a NEW upstream tool is auto-denied, safe by
    construction. This asserts that property holds (nothing is neither allowed nor denied).

    It does NOT, by itself, force a human classification decision (default-deny handles a
    new tool safely). The genuine regression guards are: test_write_tools_are_denied
    (sentinel writes stay denied), test_read_allowlist_tools_do_not_write_state (no
    write-shaped tool in the read set), and test_read_allowlist_has_no_stale_entries
    (a read tool removed/renamed upstream).

    Scope note: two genesis reflection servers only — authoritative because the reflection
    CCInvocation sets strict_mcp_config=True (_bridge.py), so ONLY those servers load;
    test_reflection_mcp_servers_match_profile guards the server list."""
    unclassified = []
    for server, modpath in _REFLECTION_MCP_SERVERS:
        for name in _registered_mcp_tool_names(modpath):
            allowed = name in _REFLECTION_READ_MCP or name in _REFLECTION_WRITE_MCP
            denied = f"mcp__{server}__{name}" in disallowed
            if not (allowed or denied):
                unclassified.append(f"{server}:{name}")
    assert not unclassified, f"unclassified reflection tools (classify read/deny): {unclassified}"


def test_read_allowlist_has_no_stale_entries():
    """Every name in the read-allowlist must exist in the live registry (catch
    typos / tools renamed or removed upstream)."""
    live = set()
    for _server, modpath in _REFLECTION_MCP_SERVERS:
        live.update(_registered_mcp_tool_names(modpath))
    stale = sorted(n for n in (_REFLECTION_READ_MCP | _REFLECTION_WRITE_MCP) if n not in live)
    assert not stale, f"read/write allowlist names not in live registry: {stale}"


def test_read_allowlist_contains_no_known_write():
    """Guard against a write tool sneaking into the read-allowlist."""
    known_write_names = {t.split("__")[-1] for t in _MUST_DENY_MCP}
    leaked = _REFLECTION_READ_MCP & known_write_names
    assert not leaked, f"write tools present in the read-allowlist: {leaked}"


# Read tools whose @mcp.tool() body contains a write-shaped token but is verified
# read-only (benign side effect, e.g. audit logging). Keep SHORT + justified.
_READ_DESPITE_WRITE_TOKEN: dict[str, str] = {}
_WRITE_TOKENS = (
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "_persist",
    ".create(",
    ".update_",
    ".set_",
    ".store(",
    ".delete(",
    ".upsert(",
    ".ingest",
    ".mark_",
)


def _dec_name(node):
    if isinstance(node, ast.Attribute):
        base = node.value.id if isinstance(node.value, ast.Name) else ""
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _mcp_tool_sources() -> dict[str, str]:
    """Map @mcp.tool()-decorated tool name -> its function source segment."""
    out: dict[str, str] = {}
    repo = pathlib.Path(__file__).resolve().parents[2]
    for d in ("src/genesis/mcp/health", "src/genesis/mcp/memory"):
        for f in (repo / d).glob("*.py"):
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                is_tool = any(
                    _dec_name(dec.func if isinstance(dec, ast.Call) else dec) == "mcp.tool"
                    for dec in node.decorator_list
                )
                if is_tool:
                    out[node.name] = ast.get_source_segment(src, node) or ""
    return out


def test_read_allowlist_tools_do_not_write_state():
    """Static guardrail: no read-allowlisted MCP tool's own @mcp.tool() body performs
    a state write. Catches a 'getter-shaped setter' (like session_config) being
    misclassified into the read set — the exact BLOCKER a hand-curated sentinel missed.
    (Limitation: sees the decorated body, not writes delegated to a helper in another
    module — those still rely on the sentinel + coverage tests.)"""
    sources = _mcp_tool_sources()
    offenders = []
    for name in sorted(_REFLECTION_READ_MCP):
        body = sources.get(name)
        if body is None or name in _READ_DESPITE_WRITE_TOKEN:
            continue
        hits = [tok for tok in _WRITE_TOKENS if tok in body]
        if hits:
            offenders.append(f"{name}: {hits}")
    assert not offenders, (
        "read-allowlisted tools contain write-shaped calls (misclassified into the "
        f"read set, or add to _READ_DESPITE_WRITE_TOKEN with a reason): {offenders}"
    )


def test_reflection_mcp_servers_match_profile():
    """The denylist derivation iterates _REFLECTION_MCP_SERVERS; the servers actually
    loaded (under strict_mcp_config) come from _MCP_PROFILES['reflection']. If a future
    PR adds a server to the profile without adding it here, that server's tools would load
    with NONE denied. Keep them in lockstep."""
    from genesis.cc.session_config import _MCP_PROFILES, _REFLECTION_MCP_SERVERS

    assert {s for s, _ in _REFLECTION_MCP_SERVERS} == set(_MCP_PROFILES["reflection"])


def test_fail_closed_when_registry_enumeration_breaks(monkeypatch):
    """If registry enumeration fails, fall back to denying both servers wholesale
    (fail-closed) — no write tool can leak."""
    import genesis.cc.session_config as sc

    def _boom(_modpath):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(sc, "_registered_mcp_tool_names", _boom)
    d = set(sc.SessionConfigBuilder().build_reflection_disallowed())
    assert "mcp__genesis-health__*" in d
    assert "mcp__genesis-memory__*" in d
    for b in _REFLECTION_DENY_BUILTINS:
        assert b in d
