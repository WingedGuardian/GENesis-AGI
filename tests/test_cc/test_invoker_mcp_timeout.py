"""Dispatched CC sessions must carry the MCP connect timeout in their env.

The repo's ``.claude/settings.json`` raises ``MCP_TIMEOUT`` for FOREGROUND
sessions. A dispatched session cannot read it: it runs with a cwd outside any git
repo, so CC never loads the repo settings at all (``CCInvoker._build_args`` says
so in its own ``--settings`` comment, which exists for that reason). Without an
explicit env entry the whole background fleet — reflection, research, sentinel,
direct sessions — keeps CC's 30s default and can silently lose an MCP server with
nobody present to notice.

These are the lock. The two values live in different languages and cannot share a
constant, so the last test compares them directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.cc.invoker import CCInvocation, CCInvoker

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def test_dispatched_session_env_carries_mcp_timeout(invoker):
    """The plain path: a dispatched session gets the raised timeout."""
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("MCP_TIMEOUT", None)
        env = invoker._build_env(CCInvocation(prompt="hello"))

    assert "MCP_TIMEOUT" in env, (
        "dispatched sessions would run on CC's 30s default and can silently lose "
        "an MCP server with nobody watching"
    )
    assert int(env["MCP_TIMEOUT"]) > 30_000, (
        f"MCP_TIMEOUT={env['MCP_TIMEOUT']} is not above CC's 30000ms default, so it "
        "buys no headroom at all"
    )


def test_operator_mcp_timeout_wins(invoker):
    """setdefault, not assignment — an operator's own value must survive.

    Matches the convention the neighbouring bg-wait ceiling already follows. A
    plain assignment here would silently override a deliberate operator choice,
    which is the failure this asserts against.
    """
    with patch.dict("os.environ", {"MCP_TIMEOUT": "45000"}, clear=False):
        env = invoker._build_env(CCInvocation(prompt="hello"))

    assert env["MCP_TIMEOUT"] == "45000"


def test_env_overrides_beat_the_default(invoker):
    """An explicit per-invocation override is applied last and wins."""
    inv = CCInvocation(prompt="hello", env_overrides={"MCP_TIMEOUT": "90000"})
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("MCP_TIMEOUT", None)
        env = invoker._build_env(inv)

    assert env["MCP_TIMEOUT"] == "90000"


def test_dispatched_and_foreground_timeouts_agree():
    """The Python constant and the JSON setting must not drift apart.

    They cover the two halves of the same problem — foreground sessions read
    ``.claude/settings.json``, dispatched ones read the env we build — and there
    is no shared source of truth to keep them equal, because one is JSON consumed
    by CC and one is Python consumed by us. So compare them.
    """
    from genesis.cc.invoker import _MCP_CONNECT_TIMEOUT_MS

    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    from_settings = settings.get("env", {}).get("MCP_TIMEOUT")

    assert from_settings is not None, (
        "the repo settings no longer set MCP_TIMEOUT, so foreground sessions have "
        "silently gone back to CC's 30s default while dispatched ones did not"
    )
    assert from_settings == _MCP_CONNECT_TIMEOUT_MS, (
        f"foreground settings.json says {from_settings} but dispatched sessions get "
        f"{_MCP_CONNECT_TIMEOUT_MS} — the two halves have drifted"
    )
