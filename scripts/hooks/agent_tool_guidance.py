#!/usr/bin/env python3
"""Retired PreToolUse hook (Agent): its nudge ships as a lesson.

The advice now lives in ``config/lessons/agent-tool-guidance.yaml`` and is
delivered by ``scripts/procedure_advisor.py``. This stub stays so a stale
``.claude/settings.json`` (a worktree branched before the retirement, or a
session started before the deploy) that still names this script does not error
on every Agent call. It does nothing. Safe to delete after 2026-11-15.
"""

import sys

if __name__ == "__main__":
    sys.exit(0)
