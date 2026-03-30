#!/usr/bin/env python3
"""PreToolUse hook — surfaces relevant procedures as advisory context.

Reads stdin JSON from CC (contains tool_name and tool_input), matches against
the YAML trigger cache, and outputs JSON with additionalContext if a procedure
matches. Silent (no output, exit 0) when no procedure matches.

Output format (CC PreToolUse hook contract):
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": "PROCEDURE: ..."
  }
}

The additionalContext field is injected into Claude's context for the current
tool call. Stdout text without this JSON structure is silently discarded.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parent.parent / "config" / "procedure_triggers.yaml"


def _load_triggers() -> list[dict]:
    """Load YAML trigger cache. Returns empty list on any error."""
    try:
        import yaml
        data = yaml.safe_load(_CACHE_PATH.read_text())
        return data.get("triggers", []) if data else []
    except Exception:
        return []


def _match_context(tool_input_str: str, context_patterns: list[str]) -> bool:
    """Check if any context pattern matches the tool input."""
    lower = tool_input_str.lower()
    for pattern in context_patterns:
        if pattern.lower() in lower:
            return True
        try:
            if re.search(pattern, tool_input_str, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False


def _format_procedure(trigger: dict) -> str:
    """Format a procedure trigger as advisory text."""
    lines = [
        f"PROCEDURE: {trigger['task_type']} (confidence: {trigger['confidence']:.0%})",
        f"Principle: {trigger['principle']}",
        "Steps:",
    ]
    for step in trigger.get("steps", []):
        lines.append(f"  - {step}")
    return "\n".join(lines)


def main() -> int:
    # Read tool call JSON from stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0  # Can't parse — fail open, silent

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Extract the semantically relevant field per tool type, not the entire JSON.
    # Matching against the whole blob causes false positives when commit messages,
    # code review text, or string literals contain trigger patterns.
    # Extract semantically relevant fields per tool type to avoid false positives
    # from commit messages, string literals, or other incidental text.
    if isinstance(tool_input, dict):
        if tool_name == "Bash":
            tool_input_str = tool_input.get("command", "")
        elif tool_name == "WebFetch":
            tool_input_str = tool_input.get("url", "")
        elif tool_name == "WebSearch":
            tool_input_str = tool_input.get("query", "")
        elif tool_name in ("Write", "Edit"):
            # Include content fields for code pattern matching
            parts = [tool_input.get("file_path", "")]
            for key in ("content", "new_string"):
                if key in tool_input:
                    parts.append(tool_input[key])
            tool_input_str = "\n".join(parts)
        else:
            tool_input_str = json.dumps(tool_input)
    else:
        tool_input_str = str(tool_input)

    # Load trigger cache
    triggers = _load_triggers()
    if not triggers:
        return 0  # No triggers — silent pass-through

    # Match tool name + context patterns
    matched = []
    for trigger in triggers:
        tools = trigger.get("tool", [])
        if isinstance(tools, str):
            tools = [tools]
        if tool_name not in tools:
            continue
        if _match_context(tool_input_str, trigger.get("context_patterns", [])):
            matched.append(trigger)

    if not matched:
        return 0  # No match — silent pass-through

    # Format advisory text from all matched procedures
    advice_parts = [_format_procedure(t) for t in matched]
    advice = "\n\n".join(advice_parts)

    # Output CC hook JSON with additionalContext
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": advice,
        }
    }
    json.dump(output, sys.stdout)
    with __import__("contextlib").suppress(BrokenPipeError):
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
