#!/usr/bin/env python3
"""Launch a Genesis MCP server for an external Codex client.

Codex is intentionally an MCP client, not a Genesis conversation.  In
particular, it must not inherit the environment markers that make a child look
like a Claude Code or Genesis-dispatched session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SESSION_CONTEXT = (
    "GENESIS_CC_SESSION",
    "GENESIS_SESSION_ID",
    "GENESIS_SESSION_ORIGIN",
    "GENESIS_SESSION_SUPERVISED",
    "GENESIS_SLOT",
    "GENESIS_TRACE_ID",
    "GENESIS_PARENT_SPAN_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment without inherited Genesis session context."""
    environment = dict(os.environ if source is None else source)
    for marker in _SESSION_CONTEXT:
        environment.pop(marker, None)
    environment["GENESIS_REPO_ROOT"] = str(runtime_root())
    return environment


def runtime_root(root: Path | None = None) -> Path:
    """Resolve the main checkout that owns Genesis's live runtime state."""
    checkout = (root or Path(__file__).resolve().parent.parent).resolve()
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return checkout
    if result.returncode != 0 or not result.stdout.strip():
        return checkout
    common_dir = Path(result.stdout.strip()).resolve()
    return common_dir.parent if common_dir.name == ".git" else checkout


def launcher_path() -> Path:
    """Locate the existing portable Genesis standalone-MCP launcher."""
    root = Path(__file__).resolve().parent.parent
    return root / ".claude" / "mcp" / "run-mcp-server"


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    launcher = launcher_path()
    if not launcher.is_file():
        raise SystemExit(f"Genesis MCP launcher not found: {launcher}")
    os.execve(str(launcher), [str(launcher), *args], sanitized_environment())  # noqa: S606


if __name__ == "__main__":
    main()
