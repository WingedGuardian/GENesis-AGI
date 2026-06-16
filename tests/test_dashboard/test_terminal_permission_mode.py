"""Interactive CC consoles default to auto permission mode, with an opt-in bypass.

WS-20 made the dashboard web terminal and the SSH/tmux dev-console slot launch
``--permission-mode auto`` (auto-approve common ops, prompt the human on gated
ones, keep deny-rule safety) instead of ``--dangerously-skip-permissions``.

This follow-up keeps ``auto`` as the default but lets an operator opt back into
bypass per environment via ``GENESIS_CC_PERMISSION_MODE=bypass`` — for
friction-free interactive sessions — without editing tracked files.

Headless/autonomous sessions (every path through ``CCInvoker``, which hardcodes
``-p``) intentionally KEEP bypass — there is no human to answer a prompt — and
are deliberately out of scope here.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from genesis.dashboard.routes.terminal import _TERMINAL_PAGE_HTML, _cc_launch_command

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_launch_command_defaults_to_auto():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GENESIS_CC_PERMISSION_MODE", None)
        assert _cc_launch_command() == "claude --permission-mode auto"


def test_launch_command_bypass_optin():
    with mock.patch.dict(os.environ, {"GENESIS_CC_PERMISSION_MODE": "bypass"}):
        assert _cc_launch_command() == "claude --dangerously-skip-permissions"


def test_launch_command_unknown_value_falls_back_to_auto():
    with mock.patch.dict(os.environ, {"GENESIS_CC_PERMISSION_MODE": "wat"}):
        assert _cc_launch_command() == "claude --permission-mode auto"


def test_terminal_prefill_is_templated():
    # The prefill must be a Jinja placeholder so the mode toggle actually applies,
    # not a hardcoded literal.
    assert 'ws.send("{{ launch_cmd | e }}")' in _TERMINAL_PAGE_HTML
    assert 'ws.send("claude --permission-mode auto")' not in _TERMINAL_PAGE_HTML
    assert 'ws.send("claude --dangerously-skip-permissions")' not in _TERMINAL_PAGE_HTML


def test_cc_slot_supports_default_auto_and_bypass_optin():
    cc_slot = (_REPO_ROOT / "scripts" / "cc-slot.sh").read_text()
    # Default branch keeps auto — WS-20's safety default is preserved.
    assert "--permission-mode auto" in cc_slot
    # Opt-in branch restores bypass.
    assert "--dangerously-skip-permissions" in cc_slot
    # Gated on the env var, with an optional sourced override (SSH RemoteCommand
    # does not source .bashrc, so a plain env var alone is not operator-settable).
    assert "GENESIS_CC_PERMISSION_MODE" in cc_slot
    assert "cc-slot.env" in cc_slot
