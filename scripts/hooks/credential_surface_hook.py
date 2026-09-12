#!/usr/bin/env python3
"""PreToolUse hook: surface stored credentials for auth-related Bash commands.

When a Bash command involves SSH, SCP, Incus, or other auth-related operations,
check the reference store and network topology for matching entries and point
the model at them so it doesn't guess.

Privacy contract: this hook POINTS at credentials, it does not reveal them. It
surfaces reference-store matches by their CONCEPT (label) name only — never the
body, which holds the raw ``Value:`` secret — and tells the model to retrieve
them via the ``reference_lookup`` MCP tool (masked, audited). Network-topology
hints are scrubbed of any inline secret shapes. Output travels on the documented
PreToolUse ``hookSpecificOutput.additionalContext`` JSON channel (plain stdout
does not reach the model on PreToolUse). Exit 0 always — advisory, never blocks.
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

# Self-locate the hooks dir so the shared helpers resolve as a script.
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
from hook_input import field, read_payload  # noqa: E402
from secret_scrub import scrub  # noqa: E402

_NETWORK_TOPOLOGY = (
    Path.home() / ".claude/projects/-home-ubuntu-genesis/memory/reference_network_topology.md"
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


def _reference_concept_hits(targets: list[str]) -> list[str]:
    """CONCEPT (label) names of reference entries matching any target.

    Never returns bodies — a reference body holds the raw ``Value:`` secret.
    The model is pointed at the ``reference_lookup`` MCP tool (masked, audited)
    instead of having the secret injected into the transcript.
    """
    db_path = _genesis_db_path()
    if not db_path.exists():
        return []

    concepts: list[str] = []
    try:
        # mode=ro is WAL-aware read-only (immutable=1 would miss un-checkpointed
        # writes); query_only is belt-and-suspenders against any write.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.row_factory = sqlite3.Row
            for target in targets:
                rows = conn.execute(
                    "SELECT DISTINCT concept FROM knowledge_units "
                    "WHERE project_type = 'reference' "
                    "AND (body LIKE ? OR concept LIKE ?) "
                    "LIMIT 3",
                    (f"%{target}%", f"%{target}%"),
                ).fetchall()
                for row in rows:
                    concept = (row["concept"] or "").strip()
                    if concept:
                        concepts.append(concept)
        finally:
            conn.close()
    except Exception:
        pass
    return concepts


def _search_network_topology(targets: list[str]) -> list[str]:
    """Search the network topology reference for target info (secret-scrubbed)."""
    if not _NETWORK_TOPOLOGY.exists():
        return []

    hints = []
    try:
        content = _NETWORK_TOPOLOGY.read_text()
        for target in targets:
            for line in content.split("\n"):
                if target in line and len(line.strip()) > 5:
                    hints.append(scrub(line.strip()[:200]))
    except Exception:
        pass
    return hints


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        norm = item.strip()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def main() -> None:
    # Skip background sessions
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    payload = read_payload()
    command = field(payload, "command")

    if not command or not _is_auth_command(command):
        return

    targets = _extract_targets(command)
    if not targets:
        return

    concept_hits = _dedupe(_reference_concept_hits(targets))
    topo_hints = _dedupe(_search_network_topology(targets))

    lines: list[str] = []
    if concept_hits:
        lines.append(
            "Stored credentials/access info exist in the reference store for: "
            + ", ".join(concept_hits[:5])
            + ". Retrieve them with the reference_lookup MCP tool — do NOT guess "
            "or reuse values from history."
        )
    if topo_hints:
        lines.append("Network topology notes for the target(s):")
        lines.extend(f"  {hint}" for hint in topo_hints[:5])

    if not lines:
        return

    context = "[Credential surface] " + "\n".join(lines)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
