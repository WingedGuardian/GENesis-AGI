"""Tests for scripts/content_safety_hook.py — PostToolUse advisory hook.

Runs the hook as a subprocess with JSON piped to stdin, verifying:
- Always exits 0 (advisory only, never blocks)
- Outputs JSON with additionalContext when a web content tool is used
- Outputs nothing for non-web tools
- Output JSON structure matches CC PostToolUse hook contract
- Resilient to malformed input
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "content_safety_hook.py"
_PYTHON = sys.executable
_TIMEOUT = 10


def _run_hook(payload: dict | str | None = None) -> subprocess.CompletedProcess:
    """Run the content_safety_hook.py script with optional JSON payload on stdin."""
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload) if payload is not None else ""
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def _parse_output(result: subprocess.CompletedProcess) -> dict | None:
    """Parse JSON output, return None if stdout is empty."""
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Matching tools — advisory injected
# ---------------------------------------------------------------------------


class TestWebContentToolsMatch:
    """Web content tools should trigger the content safety advisory."""

    def test_webfetch_any_url(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "CONTENT SAFETY" in ctx

    def test_browser_navigate(self) -> None:
        payload = {"tool_name": "browser_navigate", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_browser_snapshot(self) -> None:
        payload = {"tool_name": "browser_snapshot", "tool_input": {}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_browser_click(self) -> None:
        payload = {"tool_name": "browser_click", "tool_input": {"selector": "#link"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_browser_evaluate(self) -> None:
        payload = {"tool_name": "browser_evaluate", "tool_input": {"expression": "document.title"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_browser_run_code(self) -> None:
        payload = {"tool_name": "browser_run_code", "tool_input": {"code": "console.log('hi')"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# Non-matching tools — silent
# ---------------------------------------------------------------------------


class TestNonWebToolsSilent:
    """Non-web tools should produce no output."""

    def test_read_silent(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/foo.txt"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_bash_silent(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_write_silent(self) -> None:
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/f.py", "content": "x=1"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_edit_silent(self) -> None:
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.py", "old_string": "a", "new_string": "b"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_grep_silent(self) -> None:
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "foo"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_agent_silent(self) -> None:
        payload = {"tool_name": "Agent", "tool_input": {"task": "do something"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Output format validation
# ---------------------------------------------------------------------------


class TestOutputFormat:
    """Verify the JSON output matches CC PostToolUse hook contract exactly."""

    def test_output_structure(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None

        # Top-level key
        assert "hookSpecificOutput" in output
        hook = output["hookSpecificOutput"]

        # Required fields
        assert hook["hookEventName"] == "PostToolUse"
        assert isinstance(hook["additionalContext"], str)
        assert len(hook["additionalContext"]) > 0

    def test_no_permission_decision_or_allow(self) -> None:
        """PostToolUse advisory should not have permissionDecision, or if present, 'allow'."""
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        hook = output["hookSpecificOutput"]
        # permissionDecision should be absent (PostToolUse doesn't gate) or allow
        decision = hook.get("permissionDecision")
        assert decision is None or decision == "allow"

    def test_output_is_valid_json(self) -> None:
        payload = {"tool_name": "browser_navigate", "tool_input": {"url": "https://evil.com"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict)

    def test_advisory_mentions_prompt_injection(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "prompt injection" in ctx

    def test_advisory_mentions_system_prompt(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "system prompt" in ctx


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    """Hook must always exit 0 even under error conditions."""

    def test_invalid_json_stdin(self) -> None:
        result = _run_hook("this is not json {{{")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_empty_stdin(self) -> None:
        result = _run_hook("")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_tool_name(self) -> None:
        payload = {"tool_input": {"url": "https://example.com"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_tool_input_as_string(self) -> None:
        """tool_input as a string instead of dict should still work."""
        payload = {"tool_name": "WebFetch", "tool_input": "https://example.com"}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        # WebFetch matches by tool_name alone — tool_input content doesn't matter
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_null_tool_input(self) -> None:
        """tool_input as None should still trigger for web tools."""
        payload = {"tool_name": "browser_snapshot", "tool_input": None}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_missing_tool_input(self) -> None:
        """Missing tool_input entirely should still trigger for web tools."""
        payload = {"tool_name": "WebFetch"}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "CONTENT SAFETY" in output["hookSpecificOutput"]["additionalContext"]

    def test_none_payload(self) -> None:
        result = _run_hook(None)
        assert result.returncode == 0
        assert result.stdout.strip() == ""
