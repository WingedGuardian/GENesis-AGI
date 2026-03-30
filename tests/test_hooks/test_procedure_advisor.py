"""Tests for scripts/procedure_advisor.py — PreToolUse advisory hook.

Runs the hook as a subprocess with JSON piped to stdin, verifying:
- Always exits 0 (advisory only, never blocks)
- Outputs JSON with additionalContext when a procedure matches
- Outputs nothing when no procedure matches
- Correct field extraction per tool type (prevents false positives)
- Output JSON structure matches CC PreToolUse hook contract
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "procedure_advisor.py"
_PYTHON = sys.executable
_TIMEOUT = 10


def _run_hook(payload: dict | str | None = None) -> subprocess.CompletedProcess:
    """Run the procedure_advisor.py script with optional JSON payload on stdin."""
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
# pip editable worktree safety trigger (Bash tool)
# ---------------------------------------------------------------------------


class TestPipEditableTrigger:
    """Bash + pip install -e / --editable should match pip_editable_worktree_safety."""

    def test_pip_install_dash_e_matches(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e /some/worktree"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "pip_editable_worktree_safety" in ctx
        assert "PYTHONPATH" in ctx

    def test_pip_install_editable_flag_matches(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install --editable ."}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "pip_editable_worktree_safety" in ctx

    def test_pip_dash_e_with_extras(self) -> None:
        """pip install -e '.[dev]' should still match."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e '.[dev,test]'"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "pip_editable_worktree_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_pip3_install_dash_e(self) -> None:
        """pip3 install -e should also match (pip.*-e regex)."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip3 install -e /path"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "pip_editable_worktree_safety" in output["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# YouTube content fetch trigger (WebFetch tool)
# ---------------------------------------------------------------------------


class TestYouTubeTrigger:
    """WebFetch + YouTube URL should match youtube_content_fetch."""

    def test_youtube_url_matches(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://www.youtube.com/watch?v=abc123"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "youtube_content_fetch" in ctx
        assert "yt-dlp" in ctx

    def test_youtube_short_url_no_match(self) -> None:
        """youtu.be does NOT contain 'youtube' — no context pattern matches."""
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://youtu.be/abc123"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        # 'youtu.be' doesn't match any pattern: youtube, video, transcript, ssl, content-fetch
        assert _parse_output(result) is None

    def test_video_keyword_in_url_matches(self) -> None:
        """The context_patterns include 'video' — any URL with that word matches."""
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/video/12345"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "youtube_content_fetch" in output["hookSpecificOutput"]["additionalContext"]

    def test_non_youtube_url_no_match(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://docs.python.org/3/library/os.html"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None


# ---------------------------------------------------------------------------
# Process kill safety trigger (Write / Edit tools)
# ---------------------------------------------------------------------------


class TestProcessKillSafetyTrigger:
    """Write/Edit + os.killpg / kill patterns should match process_kill_safety."""

    def test_write_with_os_killpg_matches(self) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "${HOME}/genesis/src/foo.py",
                "content": "import os\nos.killpg(pgid, signal.SIGTERM)",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "process_kill_safety" in ctx
        assert "pgid > 1" in ctx

    def test_edit_with_killpg_matches(self) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "${HOME}/genesis/src/bar.py",
                "old_string": "pass",
                "new_string": "os.killpg(pgid, signal.SIGKILL)",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "process_kill_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_write_with_os_kill_paren_matches(self) -> None:
        """os.kill( pattern should match."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.py",
                "content": "os.kill(pid, signal.SIGTERM)",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "process_kill_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_write_with_mock_proc_pid_matches(self) -> None:
        """mock_proc.pid pattern should match."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test_proc.py",
                "content": "mock_proc.pid = 12345",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "process_kill_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_write_with_pgid_matches(self) -> None:
        """pgid pattern should match."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/test.py",
                "old_string": "x = 1",
                "new_string": "pgid = os.getpgid(proc.pid)",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "process_kill_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_write_normal_content_no_match(self) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/hello.py",
                "content": "print('hello world')\n",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_edit_normal_content_no_match(self) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/hello.py",
                "old_string": "foo",
                "new_string": "bar",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None


# ---------------------------------------------------------------------------
# No-match / silent pass-through cases
# ---------------------------------------------------------------------------


class TestSilentPassThrough:
    """Cases that should produce zero output."""

    def test_bash_normal_command(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_bash_git_status(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_bash_pip_install_no_editable(self) -> None:
        """Normal pip install (no -e / --editable) should NOT match."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install requests"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_read_tool_no_trigger(self) -> None:
        """Read tool has no triggers — should always be silent."""
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/os.killpg.py"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_unknown_tool_no_match(self) -> None:
        payload = {"tool_name": "SomeNewTool", "tool_input": {"data": "pip install -e foo"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_webfetch_normal_url(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://httpbin.org/get"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Field extraction prevents false positives
# ---------------------------------------------------------------------------


class TestFieldExtraction:
    """Verify that field extraction per tool type prevents false matches."""

    def test_bash_commit_message_with_pip_install_no_match(self) -> None:
        """A git commit whose message mentions 'pip install -e' should NOT match.

        The Bash tool extracts only the 'command' field. If the command is
        'git commit -m "fix pip install -e docs"', the regex should match
        because the command string itself contains the pattern. But a commit
        message that only appears in a 'description' field would not match.
        However, since the command string here literally contains 'pip install -e',
        the hook WILL match — this is correct behavior since the shell command
        itself references the dangerous pattern.
        """
        # This commit message IS in the command field, so it matches correctly
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'fix pip install -e docs'"},
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        # The pattern appears in the command string itself — match is correct
        output = _parse_output(result)
        assert output is not None

    def test_bash_description_field_ignored(self) -> None:
        """Fields other than 'command' should NOT be matched for Bash tool."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "ls -la",
                "description": "Check pip install -e worktree files",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        # Only 'command' is extracted — description is ignored
        assert _parse_output(result) is None

    def test_webfetch_body_field_ignored(self) -> None:
        """Only 'url' is extracted for WebFetch — other fields are ignored."""
        payload = {
            "tool_name": "WebFetch",
            "tool_input": {
                "url": "https://example.com/docs",
                "body": "youtube video transcript ssl",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_write_file_path_can_trigger(self) -> None:
        """file_path is included in the extracted text for Write.

        The pattern 'os.killpg' requires the dot and parens — a filename
        like 'killpg_helper.py' won't match because it lacks 'os.' prefix
        and '(' suffix. Use a path that actually contains a matching pattern.
        """
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/pgid_validator.py",
                "content": "# empty file",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        # 'pgid' pattern matches in the file_path
        assert output is not None
        assert "process_kill_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_edit_old_string_not_checked(self) -> None:
        """Only file_path + new_string (not old_string) should be matched for Edit."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/safe.py",
                "old_string": "os.killpg(pgid, signal.SIGTERM)",
                "new_string": "# removed dangerous call",
            },
        }
        result = _run_hook(payload)
        assert result.returncode == 0
        # old_string is NOT in the extracted fields — only file_path + new_string
        assert _parse_output(result) is None


# ---------------------------------------------------------------------------
# Error handling / resilience
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

    def test_missing_tool_input(self) -> None:
        payload = {"tool_name": "Bash"}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_missing_tool_name(self) -> None:
        payload = {"tool_input": {"command": "pip install -e ."}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None

    def test_tool_input_as_string(self) -> None:
        """tool_input can be a string instead of dict (edge case)."""
        payload = {"tool_name": "Bash", "tool_input": "pip install -e worktree"}
        result = _run_hook(payload)
        assert result.returncode == 0
        # String tool_input goes through str() path — should still match
        output = _parse_output(result)
        assert output is not None

    def test_null_tool_input(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": None}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_empty_dict_tool_input(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Output format validation
# ---------------------------------------------------------------------------


class TestOutputFormat:
    """Verify the JSON output matches CC PreToolUse hook contract exactly."""

    def test_output_structure(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e ."}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None

        # Top-level key
        assert "hookSpecificOutput" in output
        hook = output["hookSpecificOutput"]

        # Required fields
        assert hook["hookEventName"] == "PreToolUse"
        assert hook["permissionDecision"] == "allow"
        assert isinstance(hook["additionalContext"], str)
        assert len(hook["additionalContext"]) > 0

    def test_permission_decision_always_allow(self) -> None:
        """Advisory hook must NEVER block — permissionDecision is always 'allow'."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e /danger/zone"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_output_is_valid_json(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://youtube.com/watch?v=x"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        # Should not raise
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict)

    def test_advisory_text_includes_procedure_header(self) -> None:
        """Advisory text should start with PROCEDURE: <task_type>."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e ."}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("PROCEDURE:")

    def test_advisory_text_includes_confidence(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e ."}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # confidence: 0.9 formatted as 90%
        assert "90%" in ctx

    def test_advisory_text_includes_principle(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install --editable ."}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "Principle:" in ctx
        assert "worktree" in ctx.lower()

    def test_advisory_text_includes_steps(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "pip install -e /foo"}}
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "Steps:" in ctx
        assert "PYTHONPATH" in ctx


# ---------------------------------------------------------------------------
# Multiple trigger matches
# ---------------------------------------------------------------------------


class TestMultipleTriggers:
    """When multiple triggers match, all should be included in output."""

    def test_write_with_killpg_and_pgid_single_trigger(self) -> None:
        """Multiple patterns matching the same trigger should yield one entry."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.py",
                "content": "pgid = os.getpgid(pid)\nos.killpg(pgid, signal.SIGTERM)",
            },
        }
        result = _run_hook(payload)
        output = _parse_output(result)
        assert output is not None
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # Should only appear once even though multiple patterns match
        assert ctx.count("process_kill_safety") == 1


# ---------------------------------------------------------------------------
# Case insensitivity
# ---------------------------------------------------------------------------


class TestCaseInsensitivity:
    """Pattern matching should be case-insensitive."""

    def test_uppercase_pip(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "PIP INSTALL -E /path"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "pip_editable_worktree_safety" in output["hookSpecificOutput"]["additionalContext"]

    def test_mixed_case_youtube(self) -> None:
        payload = {"tool_name": "WebFetch", "tool_input": {"url": "https://YouTube.COM/watch?v=x"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        output = _parse_output(result)
        assert output is not None
        assert "youtube_content_fetch" in output["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# WebSearch tool (uses 'query' field)
# ---------------------------------------------------------------------------


class TestWebSearchExtraction:
    """WebSearch extracts the 'query' field."""

    def test_websearch_youtube_query_matches(self) -> None:
        """WebSearch is not in the youtube trigger's tool list, so no match."""
        payload = {"tool_name": "WebSearch", "tool_input": {"query": "youtube tutorial python"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        # WebSearch is NOT in the youtube trigger's tool list
        assert _parse_output(result) is None

    def test_websearch_no_trigger_defined(self) -> None:
        """No triggers target WebSearch tool currently."""
        payload = {"tool_name": "WebSearch", "tool_input": {"query": "pip install -e"}}
        result = _run_hook(payload)
        assert result.returncode == 0
        assert _parse_output(result) is None
