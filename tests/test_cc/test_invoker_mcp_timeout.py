"""Dispatched CC sessions must carry the MCP timeout in their env.

The repo's ``.claude/settings.json`` raises ``MCP_TIMEOUT`` for sessions that read
it. MOST dispatched sessions do not: they run with a cwd outside any git repo, so
CC never loads the repo settings, and the background fleet — reflection, research,
sentinel, direct sessions — would keep CC's 30s default and could silently lose an
MCP server with nobody present to notice.

Not an absolute: a worktree-cwd dispatch (``autonomy/executor/review.py``) DOES
load repo settings, as ``invoker._build_args`` says in its own ``--settings``
comment. Both halves carry the same number, so those paths agree either way — the
last test here is what keeps that true.

Scope note on ``MCP_TIMEOUT`` itself: it bounds the server CONNECT, which is the
failure this ships for, but MEASURED in CC 2.1.246 the same getter also bounds
tools/list, resource reads, the mcp_tool hook cap and the subscriptions stream. CC
has a SEPARATE ``MCP_CONNECT_TIMEOUT_MS`` (default 5000ms); these tests are not
about that one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.cc.invoker import CCInvocation, CCInvoker

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def test_dispatched_session_env_carries_mcp_timeout(invoker):
    """The plain path: a dispatched session gets the raised timeout.

    Verify-RED: deleting the ``env.setdefault("MCP_TIMEOUT", ...)`` line in
    ``_build_env`` turns this red. That line and ``env_overrides`` are the only
    writers of the key in the tree, and the fixture clears it from the inherited
    environment first, so its absence is genuinely attributable to the mechanism.
    """
    with patch.dict("os.environ", {}, clear=False):
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


def test_operator_value_survives_build_env(invoker):
    """``setdefault``, not assignment — an operator's own value must survive.

    Scoped to what it proves: this is a claim about the env dict ``_build_env``
    RETURNS, not about the environment the CC process ends up with. On a
    worktree-cwd dispatch CC additionally applies ``.claude/settings.json`` over
    the inherited environment, so the repo value wins there whatever we set.

    Verify-RED: changing the ``setdefault`` to a plain assignment turns this red.
    """
    with patch.dict("os.environ", {"MCP_TIMEOUT": "45000"}, clear=False):
        env = invoker._build_env(CCInvocation(prompt="hello"))

    assert env["MCP_TIMEOUT"] == "45000"


def test_dispatched_and_foreground_timeouts_agree():
    """The Python constant and the JSON setting must not drift apart.

    They cover the two halves of the same problem — sessions that read
    ``.claude/settings.json`` and dispatched ones that read the env we build —
    and there is no shared source of truth to keep them equal, because one is
    JSON consumed by CC and one is Python consumed by us. So compare them.

    Deliberately a string comparison. CC parses the value as an int and would
    accept a JSON number, but a type change in settings.json is exactly the kind
    of edit worth failing on rather than absorbing — do not "fix" this by
    loosening it to ``int(...) == int(...)``.
    """
    from genesis.cc.invoker import _MCP_TIMEOUT_MS

    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    from_settings = settings.get("env", {}).get("MCP_TIMEOUT")

    assert from_settings is not None, (
        "the repo settings no longer set MCP_TIMEOUT, so sessions that read them "
        "have silently gone back to CC's 30s default while dispatched ones did not"
    )
    assert from_settings == _MCP_TIMEOUT_MS, (
        f"settings.json says {from_settings!r} but dispatched sessions get "
        f"{_MCP_TIMEOUT_MS!r} — the two halves have drifted"
    )


# NOTE: no test here for "env_overrides is applied last". An earlier draft had
# one, and it was vacuous: `setdefault` yields to ANY pre-existing value, so the
# assertion held whether or not the overrides were applied last. The real lock
# already exists as test_invoker.py::test_build_env_applies_env_overrides_last,
# which overrides keys _build_env sets UNCONDITIONALLY — the only shape that can
# actually distinguish applied-last from dict-merge.
