#!/usr/bin/env python3
"""PreToolUse hook: surface credentials for auth-related Bash commands.

When a Bash command involves SSH, SCP, Incus, or other auth-related
operations, check the reference store and network topology for matching
credentials and inject them so the model doesn't guess.

Reads hook input from stdin as JSON:
  {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}

Output (stdout): credential hints injected into conversation.
Exit 0 always — this is advisory, never blocks.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Patterns that suggest the command involves authentication/remote access
_AUTH_PATTERNS = [
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
    re.compile(r"\bsftp\b"),
    re.compile(r"\bincus\b"),
    re.compile(r"\blxc\b"),
    re.compile(r"\bcurl\b.*(?:-u|--user|Bearer|Authorization|-H\s)", re.IGNORECASE),
    re.compile(r"\bgh\s+(?:auth|api)\b"),
    re.compile(r"\bgit\s+(?:push|clone|pull|fetch)\b"),
    re.compile(r"\bdocker\s+(?:login|push|pull)\b"),
    re.compile(r"\bpsql\b"),
    re.compile(r"\bmysql\b"),
]

# IP pattern to extract targets from commands
_IP_PATTERN = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_HOSTNAME_PATTERN = re.compile(r"@([\w.\-]+)")

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_NETWORK_TOPOLOGY = (
    Path.home()
    / ".claude/projects/-home-ubuntu-genesis/memory/reference_network_topology.md"
)


def _genesis_db_path() -> Path:
    """Resolve DB path via genesis.env (works in worktrees)."""
    try:
        import importlib
        return importlib.import_module("genesis.env").genesis_db_path()
    except Exception:
        return REPO_DIR / "data" / "genesis.db"  # fallback


def _is_auth_command(command: str) -> bool:
    """Check if command matches any auth pattern."""
    return any(p.search(command) for p in _AUTH_PATTERNS)


def _extract_targets(command: str) -> list[str]:
    """Extract IPs and hostnames from command."""
    targets = []
    targets.extend(_IP_PATTERN.findall(command))
    targets.extend(_HOSTNAME_PATTERN.findall(command))
    return targets


def _search_reference_store(targets: list[str]) -> list[str]:
    """Query knowledge_units for reference entries matching targets."""
    db_path = _genesis_db_path()
    if not db_path.exists():
        return []

    hints = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        try:
            for target in targets:
                rows = conn.execute(
                    "SELECT concept, body FROM knowledge_units "
                    "WHERE project_type = 'reference' "
                    "AND (body LIKE ? OR concept LIKE ?) "
                    "LIMIT 3",
                    (f"%{target}%", f"%{target}%"),
                ).fetchall()
                for row in rows:
                    body = row["body"] or ""
                    # Extract the most useful line (the one with the target)
                    for line in body.split("\n"):
                        if target in line and len(line.strip()) > 10:
                            hints.append(line.strip()[:200])
                            break
                    else:
                        if body:
                            hints.append(body[:200])
        finally:
            conn.close()
    except Exception:
        pass
    return hints


def _search_network_topology(targets: list[str]) -> list[str]:
    """Search the network topology reference for target info."""
    if not _NETWORK_TOPOLOGY.exists():
        return []

    hints = []
    try:
        content = _NETWORK_TOPOLOGY.read_text()
        for target in targets:
            for line in content.split("\n"):
                if target in line and len(line.strip()) > 5:
                    hints.append(line.strip()[:200])
    except Exception:
        pass
    return hints


def main() -> None:
    # Skip background sessions
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command or not _is_auth_command(command):
        return

    targets = _extract_targets(command)
    if not targets:
        return

    # Check both sources
    ref_hints = _search_reference_store(targets)
    topo_hints = _search_network_topology(targets)

    all_hints = []
    seen = set()
    for hint in ref_hints + topo_hints:
        normalized = hint.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            all_hints.append(normalized)

    if all_hints:
        print("[Credential] Found stored access info for target:")
        for hint in all_hints[:5]:
            print(f"  {hint}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
