#!/usr/bin/env python3
"""PreToolUse hook — blocks Write/Edit to CRITICAL protected paths.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads CLAUDE_TOOL_INPUT JSON from stdin, extracts file_path,
checks against CRITICAL patterns from config/protected_paths.yaml.

Exit codes:
  0 — allow (path is not CRITICAL)
  2 — block (path is CRITICAL, cannot be modified from this channel)

Emits SteerMessage for unified enforcement feedback.
"""

import json
import sys
from fnmatch import fnmatch
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "protected_paths.yaml"

# Hardcoded fallback — protects the most dangerous paths even when config is
# missing or corrupted.  Fail-closed: if we can't load the full config, at
# least these patterns are still enforced.
_FALLBACK_CRITICAL = [
    "*/secrets.env",
    ".claude/settings.json",
    "src/genesis/autonomy/protection.py",
    "config/protected_paths.yaml",
]


def _load_critical_patterns() -> list[str]:
    """Load CRITICAL path patterns from config, falling back to hardcoded list."""
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except (OSError, yaml.YAMLError) as exc:
        print(
            f"WARNING: protected_paths.yaml load failed ({exc}), using fallback",
            file=sys.stderr,
        )
        return list(_FALLBACK_CRITICAL)
    patterns = []
    for rule in data.get("critical", []):
        patterns.append(rule["pattern"])
    return patterns


def _matches(path: str, patterns: list[str]) -> str | None:
    """Return the matching pattern if path matches any CRITICAL pattern, else None."""
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch(normalized, pattern):
            return pattern
        # Handle ** recursive glob
        if "**" in pattern:
            prefix = pattern.split("**")[0]
            if normalized.startswith(prefix):
                return pattern
    return None


def main() -> int:
    tool_input = sys.stdin.read()
    try:
        data = json.loads(tool_input)
    except json.JSONDecodeError as exc:
        print(f"WARNING: pretool_check stdin parse failed ({exc})", file=sys.stderr)
        return 0  # Can't parse — fail open

    file_path = data.get("file_path", "")
    if not file_path:
        return 0

    patterns = _load_critical_patterns()
    matched = _matches(file_path, patterns)
    if matched:
        from genesis.autonomy.steering import SteerMessage
        from genesis.autonomy.types import ApprovalDecision, EnforcementLayer

        msg = SteerMessage(
            layer=EnforcementLayer.PERMISSION_GATE,
            rule_id="critical_protected_path",
            decision=ApprovalDecision.BLOCK,
            severity="critical",
            title="CRITICAL protected path",
            context=f"Matches pattern '{matched}'",
            suggestion="This path cannot be modified from a relay/chat channel. "
                       "Use a direct CC CLI session instead.",
            tool_name="Write",
            file_path=file_path,
        )
        print(msg.to_stderr(), file=sys.stderr)
        return msg.to_exit_code()

    return 0


if __name__ == "__main__":
    sys.exit(main())
