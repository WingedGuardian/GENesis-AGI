"""WS-20: interactive CC sessions launch in auto permission mode, not bypass.

The dashboard web terminal prefill and the SSH/tmux dev-console slot each spawn
an INTERACTIVE Claude Code session that a human drives, so they use
``--permission-mode auto`` (auto-approve common ops, prompt the human on gated
ones, keep deny-rule safety) instead of ``--dangerously-skip-permissions``.

Headless/autonomous sessions (every path through ``CCInvoker``, which hardcodes
``-p``) intentionally KEEP bypass — there is no human to answer a prompt — and
are deliberately out of scope for this test.
"""

from __future__ import annotations

from pathlib import Path

from genesis.dashboard.routes.terminal import _TERMINAL_PAGE_HTML

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_terminal_prefill_uses_auto_mode():
    # Assert the *launched command* (not comment text) prefills auto mode.
    assert 'ws.send("claude --permission-mode auto")' in _TERMINAL_PAGE_HTML
    assert 'ws.send("claude --dangerously-skip-permissions")' not in _TERMINAL_PAGE_HTML


def test_cc_slot_launches_auto_mode():
    # cc-slot.sh launches the interactive tmux dev console a human attaches to.
    cc_slot = (_REPO_ROOT / "scripts" / "cc-slot.sh").read_text()
    assert "exec claude --permission-mode auto" in cc_slot
    assert "exec claude --dangerously-skip-permissions" not in cc_slot
