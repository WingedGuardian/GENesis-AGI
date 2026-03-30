#!/usr/bin/env python3
"""Behavioral linter — enforces anti-pattern rules on Write/Edit operations.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads CLAUDE_TOOL_INPUT JSON from stdin, loads all rule YAML files from
config/behavioral_rules/, and checks the content being written.

Exit codes:
  0 — allow (no rule violations, or only warnings)
  2 — block (a rule with severity=block matched)

Escape hatch: Add a comment containing 'behavioral-lint: ignore <rule-name>'
in the content to suppress a specific rule for that file. This leaves an
audit trail — the user approved the exception.
"""

import json
import re
import sys
from pathlib import Path

import yaml

_RULES_DIR = Path(__file__).resolve().parent.parent / "config" / "behavioral_rules"


def _load_rules() -> list[dict]:
    """Load all rule YAML files from the behavioral_rules directory."""
    rules = []
    if not _RULES_DIR.is_dir():
        return rules
    for f in sorted(_RULES_DIR.glob("*.yaml")):
        try:
            rule = yaml.safe_load(f.read_text())
            if rule and isinstance(rule, dict) and "patterns" in rule:
                rules.append(rule)
        except Exception as exc:
            print(f"WARNING: Failed to load behavioral rule {f.name}: {exc}", file=sys.stderr)
    return rules


def _check_content(content: str, rules: list[dict]) -> list[tuple[dict, dict]]:
    """Check content against all rules. Returns list of (rule, matched_pattern) tuples."""
    violations = []
    for rule in rules:
        rule_name = rule.get("name", "unnamed")

        # Check for escape hatch
        escape = f"behavioral-lint: ignore {rule_name}"
        if escape in content:
            continue

        for pattern_def in rule.get("patterns", []):
            regex = pattern_def.get("regex", "")
            if not regex:
                continue
            try:
                if re.search(regex, content, re.IGNORECASE | re.MULTILINE):
                    violations.append((rule, pattern_def))
                    break  # One match per rule is enough
            except re.error:
                print(f"WARNING: Invalid regex in rule {rule_name}: {regex}", file=sys.stderr)
    return violations


def main() -> int:
    tool_input = sys.stdin.read()
    try:
        data = json.loads(tool_input)
    except json.JSONDecodeError as exc:
        print(f"WARNING: behavioral_linter stdin parse failed ({exc})", file=sys.stderr)
        return 0  # Can't parse — fail open

    # Extract the content being written
    content = data.get("content", "") or data.get("new_string", "")
    if not content:
        return 0  # No content to check (e.g., delete operation)

    file_path = data.get("file_path", "")

    rules = _load_rules()
    if not rules:
        return 0

    violations = _check_content(content, rules)
    if not violations:
        return 0

    # Report violations
    exit_code = 0
    for rule, pattern_def in violations:
        severity = rule.get("severity", "warn")
        name = rule.get("name", "unnamed")
        context = pattern_def.get("context", "")
        suggestion = rule.get("suggestion", "")

        msg = (
            f"\n{'BLOCKED' if severity == 'block' else 'WARNING'}: "
            f"Behavioral rule '{name}' violated\n"
            f"  File: {file_path}\n"
            f"  Issue: {context}\n"
            f"  Rule: {rule.get('description', '')}\n"
            f"  Fix: {suggestion}\n"
            f"  Escape: Add '# behavioral-lint: ignore {name}' if user-approved\n"
        )
        print(msg, file=sys.stderr)

        if severity == "block":
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
