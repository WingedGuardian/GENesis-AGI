"""The one place Genesis code starts the Codex CLI.

Every Codex run Genesis launches is unattended, so it must carry the dispatched
marker: an unmarked Codex run is the owner's own interactive client, the same
way an unmarked Claude Code session is. Spawning through this module is how a
caller gets that right without knowing it; ``tests/test_codex_dispatch_marker.py``
fails when the ``codex`` name appears anywhere else under ``src/genesis``.
"""

from __future__ import annotations

import shutil

from genesis.caller_context import dispatched_child_env

_CODEX_BIN = "codex"


def codex_available() -> bool:
    """Whether the Codex CLI is on PATH."""
    return shutil.which(_CODEX_BIN) is not None


def codex_exec_argv(*args: str) -> list[str]:
    """``codex exec <args>``. Spawn it with ``env=codex_child_env()``."""
    return [_CODEX_BIN, "exec", *args]


def codex_child_env() -> dict[str, str]:
    """The environment for a Codex child Genesis starts: marked dispatched."""
    return dispatched_child_env()
