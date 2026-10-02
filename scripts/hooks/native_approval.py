#!/usr/bin/env python3
"""Render Claude Code's native PreToolUse approval decision.

This module owns the wire payload and the optional discarded-command note.  Gate
specific wrappers keep their own audit and control-flow behavior.
"""

from __future__ import annotations

import json

try:
    import discarded_write
except Exception:  # A cosmetic note must never suppress an approval prompt.
    discarded_write = None  # type: ignore[assignment]


def emit_native_ask(reason: str) -> None:
    """Print one fail-safe native ``ask`` decision to stdout."""
    if discarded_write is not None:
        try:
            extra = discarded_write.prompt_note()
        except Exception:
            extra = None
        if extra:
            reason = f"{reason}\n\n{extra}"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
