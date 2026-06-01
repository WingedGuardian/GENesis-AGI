#!/usr/bin/env python3
"""Audit feedback memory coverage against hook enforcement.

Standalone script (NOT a CC hook). Compares feedback memories stored in
the Genesis memory system against the hooks registered in settings.json
to identify coverage gaps — feedback lessons that have no corresponding
enforcement mechanism.

Three data sources:
  1. Feedback files on disk (~/.claude/projects/-home-ubuntu-genesis/memory/feedback_*.md)
  2. Rule-class memories in SQLite (memory_metadata + memory_fts)
  3. Procedures in SQLite (procedural_memory)

Outputs a JSON report to stdout with covered/uncovered/unmatched items.

Usage:
  python scripts/hooks/audit_feedback_coverage.py
  python scripts/hooks/audit_feedback_coverage.py --summary  # compact output
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# --- Configuration ---

_GENESIS_DB = Path.home() / "genesis" / "data" / "genesis.db"
_SETTINGS_JSON = Path.home() / "genesis" / ".claude" / "settings.json"
_FEEDBACK_DIR = (
    Path.home() / ".claude" / "projects" / "-home-ubuntu-genesis" / "memory"
)


def _load_feedback_files() -> list[dict]:
    """Load feedback memory files from disk."""
    results = []
    if not _FEEDBACK_DIR.exists():
        return results
    for f in sorted(_FEEDBACK_DIR.glob("feedback_*.md")):
        text = f.read_text(errors="replace")
        # Extract name from YAML frontmatter
        name_match = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        desc_match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        results.append({
            "source": "file",
            "path": str(f.name),
            "name": name_match.group(1).strip() if name_match else f.stem,
            "description": desc_match.group(1).strip() if desc_match else "",
            "content_preview": text[:300],
        })
    return results


def _load_db_rules() -> list[dict]:
    """Load rule-class memories from SQLite."""
    if not _GENESIS_DB.exists():
        return []
    try:
        with sqlite3.connect(str(_GENESIS_DB)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT m.memory_id, f.content, f.tags
                FROM memory_metadata m
                JOIN memory_fts f ON m.memory_id = f.memory_id
                WHERE m.memory_class = 'rule' AND m.deprecated = 0
                """
            ).fetchall()
            results = []
            for r in rows:
                content = r["content"] or ""
                name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
                results.append({
                    "source": "db_rule",
                    "memory_id": r["memory_id"][:12],
                    "name": name_match.group(1).strip() if name_match else "",
                    "tags": r["tags"] or "",
                    "content_preview": content[:300],
                })
            return results
    except Exception:
        return []


def _load_procedures() -> list[dict]:
    """Load active procedures from SQLite."""
    if not _GENESIS_DB.exists():
        return []
    try:
        with sqlite3.connect(str(_GENESIS_DB)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT id, task_type, principle, confidence, tool_trigger
                FROM procedural_memory
                WHERE deprecated = 0
                ORDER BY confidence DESC
                """
            ).fetchall()
            results = []
            for r in rows:
                results.append({
                    "source": "procedure",
                    "id": r["id"][:12] if r["id"] else "",
                    "task_type": r["task_type"] or "",
                    "principle": (r["principle"] or "")[:200],
                    "confidence": r["confidence"],
                    "tool_trigger": r["tool_trigger"] or "",
                })
            return results
    except Exception:
        return []


def _load_hook_inventory() -> list[dict]:
    """Parse settings.json to inventory all registered hooks."""
    if not _SETTINGS_JSON.exists():
        return []
    try:
        settings = json.loads(_SETTINGS_JSON.read_text())
    except Exception:
        return []

    hooks_config = settings.get("hooks", {})
    inventory = []

    for event_type, hook_groups in hooks_config.items():
        for group in hook_groups:
            matcher = group.get("matcher", "*")
            for hook_def in group.get("hooks", []):
                command = hook_def.get("command", "")
                # Extract the script name from the command
                script_name = ""
                if "genesis-hook" in command:
                    parts = command.split("genesis-hook")
                    if len(parts) > 1:
                        script_name = parts[1].strip()
                elif command.startswith("bash -c"):
                    # Inline bash — extract a summary
                    script_name = "(inline bash)"

                inventory.append({
                    "event": event_type,
                    "matcher": matcher,
                    "script": script_name,
                    "command_preview": command[:120],
                })
    return inventory


def _extract_enforced_patterns(hooks: list[dict]) -> set[str]:
    """Extract keywords from hook commands that indicate what they enforce."""
    patterns = set()
    for h in hooks:
        cmd = h.get("command_preview", "").lower()
        script = h.get("script", "").lower()
        # Extract meaningful keywords
        for keyword in [
            "destructive", "credential", "git_push", "force",
            "nohup", "pip install", "worktree", "protected_path",
            "review", "concurrent", "full_suite", "rm -r",
            "reset --hard", "clean -f", "sqlite3", "youtube",
            "web_tools", "stealth", "behavioral", "pretool",
        ]:
            if keyword in cmd or keyword in script:
                patterns.add(keyword)
    return patterns


def _classify_coverage(
    feedback: list[dict], hooks: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Classify feedback items as covered or uncovered by hooks.

    Heuristic: check if domain-specific keywords from the feedback
    name/description appear in hook enforcement patterns or script names.
    Filters out generic path/language words to reduce false positives.
    """
    enforced = _extract_enforced_patterns(hooks)

    # Extract only script names and inline command arguments — not file paths
    hook_terms = set()
    for h in hooks:
        script = h.get("script", "").lower()
        # Script names are meaningful (e.g., "destructive_command_guard.py")
        hook_terms.update(re.findall(r"[a-z_]{5,}", script))
        cmd = h.get("command_preview", "").lower()
        # Extract quoted strings and flag-like arguments from inline bash
        hook_terms.update(re.findall(r"'([a-z_]{5,})'", cmd))
        hook_terms.update(re.findall(r'"([a-z_]{5,})"', cmd))

    # Words too generic to be meaningful matches
    _NOISE_WORDS = {
        "hooks", "check", "guard", "genesis", "claude", "python",
        "script", "shell", "print", "error", "false",
        "return", "import", "output", "input", "should", "would",
        "could", "never", "always", "about", "their", "there",
        "which", "where", "these", "those", "other", "after",
        "before", "first", "every", "using", "because", "process",
    }

    covered = []
    uncovered = []

    for item in feedback:
        name = item.get("name", "").lower()
        desc = item.get("description", "").lower()
        combined = name + " " + desc

        # Extract meaningful domain words (5+ chars, not noise)
        words = [
            w for w in re.findall(r"[a-z_]{5,}", combined)
            if w not in _NOISE_WORDS
        ]
        match_count = sum(1 for w in words if w in hook_terms)

        if match_count >= 2 or any(p in combined for p in enforced):
            covered.append({
                **item,
                "match_strength": "strong" if match_count >= 3 else "weak",
            })
        else:
            uncovered.append(item)

    return covered, uncovered


def main() -> None:
    summary_mode = "--summary" in sys.argv

    feedback_files = _load_feedback_files()
    db_rules = _load_db_rules()
    procedures = _load_procedures()
    hooks = _load_hook_inventory()

    covered, uncovered = _classify_coverage(feedback_files, hooks)

    report = {
        "summary": {
            "feedback_files": len(feedback_files),
            "db_rules": len(db_rules),
            "procedures": len(procedures),
            "hooks_registered": len(hooks),
            "covered": len(covered),
            "uncovered": len(uncovered),
            "coverage_pct": round(
                len(covered) / max(len(feedback_files), 1) * 100, 1
            ),
        },
        "uncovered_feedback": [
            {"name": u["name"], "path": u.get("path", ""), "description": u.get("description", "")}
            for u in uncovered
        ],
    }

    if not summary_mode:
        report["covered_feedback"] = [
            {"name": c["name"], "match_strength": c["match_strength"]}
            for c in covered
        ]
        report["hooks"] = [
            {"event": h["event"], "matcher": h["matcher"], "script": h["script"]}
            for h in hooks
        ]
        report["procedures"] = [
            {"task_type": p["task_type"], "confidence": p["confidence"]}
            for p in procedures[:10]
        ]

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
