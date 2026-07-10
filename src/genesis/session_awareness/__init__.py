"""Ambient session-awareness layer (WS-C).

Watches what a CC session is *about* — an EMA over genuine user-prompt
embeddings — and detects when the theme settles (drift trigger). PR1 is
record-only: the proactive memory hook folds each turn and records fires
in ``session_theme.json``; the detached retrieval worker (PR2) and the
arbiter (PR3) act on those fires. Shadow-first: nothing here injects
anything into a session until the live-flip gate.

The hook-facing entry point is :func:`hook_fold` — one call per genuine
user turn, fail-open by contract (it never raises).
"""

from .hook_api import hook_fold

__all__ = ["hook_fold"]
