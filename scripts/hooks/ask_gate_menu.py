#!/usr/bin/env python3
"""Transition stub: the escalation-cap menu this file used to be is deleted.

The local review-round streak and its cap menu are gone, and `.claude/settings.json`
no longer wires this hook. It stays as a no-op only because hooks always run from
the main checkout while each worktree's OWN settings decide which hooks are called:
a worktree cut before the deletion still names this file on every AskUserQuestion,
and a missing script makes the launcher exit non-zero (exit 2, a refusal, in a
session with a Bash allowlist). The deleted hook was fail-open by design so it could
never stop a session asking its user something, and this stub keeps that promise.

Delete it once no live worktree's settings name it
(`grep -l ask_gate_menu .claude/worktrees/*/.claude/settings.json`).
"""

import sys

if __name__ == "__main__":
    sys.exit(0)
