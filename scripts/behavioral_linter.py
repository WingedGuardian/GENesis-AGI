#!/usr/bin/env python3
"""Behavioral linter — enforces anti-pattern rules on Write/Edit operations.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads the CC hook payload from stdin (via hook_input), loads all rule YAML files from
config/behavioral_rules/, and checks the content being written.

Exit codes:
  0 — allow (no rule violations, or only warnings)
  2 — block (a rule with severity=block matched)

Escape hatch: Add a comment containing 'behavioral-lint: ignore <rule-name>'
in the content to suppress a specific rule for that file. This leaves an
audit trail — the user approved the exception.

Emits SteerMessage for unified enforcement feedback.
"""

import json
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

import yaml

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload  # noqa: E402

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


def _severity_of(rule: dict, pattern_def: dict) -> str:
    """Resolve a pattern's severity, falling back to the rule-level default.

    Per-pattern ``severity`` lets one rule mix hard-block code patterns with
    advisory (warn) prose patterns. Anything other than "block" resolves to
    "warn" (fail toward the softer decision).
    """
    sev = pattern_def.get("severity") or rule.get("severity", "warn")
    return "block" if sev == "block" else "warn"


def _glob_match(file_path: str, globs: list) -> bool:
    """Whether ``file_path`` matches any glob (full normalized path or basename)."""
    name = file_path.replace("\\", "/")
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch(name, g) or fnmatch(base, g) for g in globs)


def _applies_to(rule: dict, file_path: str) -> bool:
    """Whether a rule applies to the given file path.

    A rule may declare ``applies_to`` (allow-list) and/or ``excludes``
    (deny-list) as lists of globs:
    - ``excludes`` wins: a file matching an exclude glob is never checked. Use
      this for a code rule that should fire everywhere EXCEPT docs (safer than
      an allow-list, which silently misses extensionless scripts / notebooks /
      templates that carry real code).
    - ``applies_to``: when present, the rule fires only for matching files.
    Absent both → applies to all. An empty/absent file_path keeps the rule
    active (fail toward checking).
    """
    excludes = rule.get("excludes")
    if excludes and file_path and _glob_match(file_path, excludes):
        return False
    globs = rule.get("applies_to")
    if not globs:
        return True
    if not file_path:
        return True
    return _glob_match(file_path, globs)


def _check_content(
    content: str, rules: list[dict], file_path: str = ""
) -> list[tuple[dict, dict, str]]:
    """Check content against all rules.

    Returns one ``(rule, pattern_def, severity)`` per violated rule, choosing
    the HIGHEST-severity matching pattern for that rule (so a warn-level pattern
    can never mask a block-level one — which the old first-match ``break`` did).
    """
    violations = []
    for rule in rules:
        rule_name = rule.get("name", "unnamed")

        if not _applies_to(rule, file_path):
            continue

        # Escape hatch: an explicit opt-out comment turns off the whole rule.
        escape = f"behavioral-lint: ignore {rule_name}"
        if escape in content:
            continue

        best: tuple[dict, dict, str] | None = None
        best_rank = -1
        for pattern_def in rule.get("patterns", []):
            regex = pattern_def.get("regex", "")
            if not regex:
                continue
            try:
                matched = re.search(regex, content, re.IGNORECASE | re.MULTILINE)
            except re.error:
                print(f"WARNING: Invalid regex in rule {rule_name}: {regex}", file=sys.stderr)
                continue
            if not matched:
                continue
            severity = _severity_of(rule, pattern_def)
            rank = 2 if severity == "block" else 0
            if rank > best_rank:
                best = (rule, pattern_def, severity)
                best_rank = rank
                if rank == 2:
                    break  # block is the max — no pattern can outrank it
        if best is not None:
            violations.append(best)
    return violations


def _plain_stderr(rule: dict, pattern_def: dict, severity: str, name: str, file_path: str) -> str:
    """Format a violation without the genesis package (fresh-install fallback).

    Mirrors SteerMessage.to_stderr()'s shape so downstream text assertions and
    the human-readable format stay stable when genesis isn't importable.
    """
    label = "BLOCKED" if severity == "block" else "WARNING"
    lines = [f"\n{label}: Behavioral rule '{name}' violated", f"  Rule: {name}"]
    if file_path:
        lines.append(f"  File: {file_path}")
    if pattern_def.get("context"):
        lines.append(f"  Issue: {pattern_def['context']}")
    fix = rule.get("description", "")
    if rule.get("suggestion"):
        fix += "\n  " + rule["suggestion"]
    if fix:
        lines.append(f"  Fix: {fix}")
    lines.append(f"  Escape: Add '# behavioral-lint: ignore {name}' if user-approved")
    return "\n".join(lines) + "\n"


def _emit(violations: list[tuple[dict, dict, str]], file_path: str) -> int:
    """Print each violation to stderr; return the max exit code (2 = block).

    Prefers SteerMessage formatting, but if the genesis package isn't importable
    (a fresh/partial install), falls back to plain text that STILL returns the
    correct exit code — so a block never silently degrades to a non-blocking
    error just because genesis wasn't on the path.
    """
    try:
        from genesis.autonomy.steering import SteerMessage
        from genesis.autonomy.types import ApprovalDecision, EnforcementLayer

        use_steer = True
    except Exception:
        use_steer = False

    exit_code = 0
    block_texts: list[str] = []
    warn_texts: list[str] = []
    for rule, pattern_def, severity in violations:
        name = rule.get("name", "unnamed")
        is_block = severity == "block"
        if use_steer:
            msg = SteerMessage(
                layer=EnforcementLayer.HARD_BLOCK,
                rule_id=name,
                decision=ApprovalDecision.BLOCK if is_block else ApprovalDecision.ACT,
                severity="critical" if is_block else "medium",
                title=f"Behavioral rule '{name}' violated",
                context=pattern_def.get("context", ""),
                suggestion=rule.get("description", "")
                + ("\n  " + rule.get("suggestion", "") if rule.get("suggestion") else ""),
                tool_name="Write",
                file_path=file_path,
                can_suppress=True,
                suppress_key=f"# behavioral-lint: ignore {name}",
            )
            text = msg.to_stderr()
            code = msg.to_exit_code()
        else:
            text = _plain_stderr(rule, pattern_def, severity, name, file_path)
            code = 2 if is_block else 0
        (block_texts if is_block else warn_texts).append(text)
        if code > exit_code:
            exit_code = code

    if exit_code == 2:
        # A block fired: exit 2 delivers stderr to the model, so surface every
        # message (blocks and any warns) there.
        for text in block_texts + warn_texts:
            print(text, file=sys.stderr)
    elif warn_texts:
        # Warn-only: PreToolUse stderr on exit 0 is DISCARDED by Claude Code, so
        # the advisory must ride hookSpecificOutput.additionalContext (stdout),
        # which is delivered to the model.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "\n".join(warn_texts),
                    }
                }
            )
        )
    return exit_code


def main() -> int:
    payload = read_payload()

    # Extract the content being written
    content = field(payload, "content") or field(payload, "new_string")
    if not content:
        return 0  # No content to check (e.g., delete operation)

    file_path = field(payload, "file_path")

    # Never lint the rule-definition files themselves: they necessarily contain
    # the very patterns they match (the kill-all call literals, the hide-on-
    # error CSS), so self-linting hard-blocks every edit to this directory — a
    # #1227 side effect once the hook gained teeth. Editing the rules is how the
    # rules get fixed; it must not require the escape hatch in each file. Match
    # the ACTUAL rules dir by resolved path (not a bare substring, which would
    # also skip an unrelated */config/behavioral_rules/* path elsewhere).
    if file_path:
        try:
            if _RULES_DIR.resolve() in Path(file_path).resolve().parents:
                return 0
        except (OSError, ValueError, RuntimeError):
            pass

    rules = _load_rules()
    if not rules:
        return 0

    violations = _check_content(content, rules, file_path)
    if not violations:
        return 0

    return _emit(violations, file_path)


if __name__ == "__main__":
    sys.exit(main())
