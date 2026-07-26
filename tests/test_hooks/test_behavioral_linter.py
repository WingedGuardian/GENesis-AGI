"""Tests for scripts/behavioral_linter.py — PreToolUse hook.

Runs the script as a subprocess with piped stdin JSON to test the actual
invocation pattern used by the CC CLI. Checks exit code and stderr output.

Exit codes:
  0 — allow (no violations, only warnings, empty content, parse failure)
  2 — block (a pattern with severity=block matched)

behavioral-lint: ignore no-hide-problems
behavioral-lint: ignore no-unguarded-kill
^ This is the linter's OWN test suite: its fixtures necessarily contain every
  trigger pattern the linter matches, so it must be exempt from self-linting.
  Post-deploy the linter also skips this by directory, but the escape tokens
  keep the file editable under an older (still-blocking) hook.
"""

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "behavioral_linter.py")
_PYTHON = sys.executable


def _run_linter(
    payload: str | dict,
    *,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess:
    """Run behavioral_linter.py as a subprocess with the given stdin payload.

    Args:
        payload: Either a raw string (for parse-failure tests) or a dict
                 that will be JSON-serialized.
        timeout: Max seconds to wait.

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return subprocess.run(
        [_PYTHON, _SCRIPT],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# no-hide-problems — code-level patterns BLOCK (exit 2)
# ---------------------------------------------------------------------------


class TestNoHideProblemsCodeBlocks:
    """Code-level hiding (display:none / x-show on an error state) → exit 2.

    These patterns require a literal ``display:none`` or an Alpine ``x-show``,
    which effectively never appear in prose, so they remain hard blocks.
    """

    def test_display_none_with_error_state(self):
        """CSS display:none on an error-named element → blocked."""
        content = ".error-widget { display: none; /* unknown state */ }"
        result = _run_linter({"content": content, "file_path": "dashboard.css"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr
        assert "BLOCKED" in result.stderr

    def test_display_none_with_failed_keyword(self):
        """display:none with 'failed' keyword → blocked."""
        content = 'style="display: none" data-state="failed"'
        result = _run_linter({"content": content, "file_path": "template.html"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr

    def test_xshow_hiding_errors(self):
        """Alpine x-show with negated error state → blocked."""
        content = '<div x-show="!hasError">Only shown when no error</div>'
        result = _run_linter({"content": content, "file_path": "component.html"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr

    def test_xshow_hiding_unknown(self):
        """Alpine x-show with negated unknown state → blocked."""
        content = '<span x-show="!isUnknown">data here</span>'
        result = _run_linter({"content": content, "file_path": "widget.html"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr

    def test_xshow_hiding_failed_compound(self):
        """Alpine x-show with compound expression negating failed → blocked."""
        content = '<div x-show="data && !isFailed">content</div>'
        result = _run_linter({"content": content, "file_path": "panel.html"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr


# ---------------------------------------------------------------------------
# no-hide-problems — reasoning/prose patterns WARN (exit 0)
# ---------------------------------------------------------------------------


class TestNoHideProblemsProseWarns:
    """Reasoning/prose proposals → WARN (exit 0), not a hard block.

    The prose patterns match token adjacency, not intent, so they surface the
    concern without blocking legitimate writing. Each still names the rule on
    stderr so the nudge is visible.
    """

    def _assert_warns(self, result):
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stderr
        assert "no-hide-problems" in result.stderr
        assert "BLOCKED" not in result.stderr

    def test_propose_hide_broken_data(self):
        content = "# We should hide the broken data for now"
        self._assert_warns(_run_linter({"content": content, "file_path": "plan.md"}))

    def test_propose_skip_unknown(self):
        content = "# skip the unknown values until we figure it out"
        self._assert_warns(_run_linter({"content": content, "file_path": "notes.py"}))

    def test_propose_suppress_failed(self):
        content = "# suppress the failed items in the output"
        self._assert_warns(_run_linter({"content": content, "file_path": "renderer.py"}))

    def test_propose_omit_stale(self):
        content = "# omit any stale entries from the dashboard"
        self._assert_warns(_run_linter({"content": content, "file_path": "dashboard.py"}))

    def test_reverse_order_error_then_hide(self):
        content = "# error state — hide it from the user"
        self._assert_warns(_run_linter({"content": content, "file_path": "view.py"}))

    def test_dont_show_error(self):
        content = "# don't show the error state to users"
        self._assert_warns(_run_linter({"content": content, "file_path": "handler.py"}))

    def test_do_not_display_unknown(self):
        content = "# do not display unknown values in the table"
        self._assert_warns(_run_linter({"content": content, "file_path": "table.py"}))

    def test_only_show_when_available(self):
        content = "# only show the metric when the data is available"
        self._assert_warns(_run_linter({"content": content, "file_path": "metrics.py"}))

    def test_only_show_when_healthy(self):
        content = "# only show the panel when the service is healthy"
        self._assert_warns(_run_linter({"content": content, "file_path": "service.py"}))

    def test_skip_showing_unless_configured(self):
        content = "# skip showing the widget unless the source exists"
        self._assert_warns(_run_linter({"content": content, "file_path": "widget.py"}))

    def test_unknown_suppress_in_comment(self):
        content = "# unknown data, just suppress it for now"
        self._assert_warns(_run_linter({"content": content, "file_path": "processor.py"}))

    def test_warns_in_new_string_field(self):
        """Prose in new_string (Edit tool) is also checked — warns."""
        result = _run_linter(
            {
                "new_string": "# hide the broken data for now",
                "old_string": "# show the broken data",
                "file_path": "editor.py",
            }
        )
        self._assert_warns(result)

    def test_identifier_embedded_not_flagged(self):
        """Regression: word-boundary anchoring — identifiers must not trip.

        The live false positives that motivated the warn downgrade + \\b anchors:
        a removed error-tuple identifier and a skip_writeback param near 'drift'.
        """
        for content in (
            "removed the now-dead _QDRANT_ERRORS tuple",
            "skip_writeback asserted after the too-small-unknown drift",
        ):
            result = _run_linter({"content": content, "file_path": "notes.py"})
            assert result.returncode == 0, f"{content!r} → {result.stderr}"
            assert "no-hide-problems" not in result.stderr


# ---------------------------------------------------------------------------
# no-unguarded-kill rule — should BLOCK (exit 2)
# ---------------------------------------------------------------------------


class TestNoUnguardedKillBlocks:
    """Content that violates no-unguarded-kill must produce exit 2."""

    def test_os_kill_minus_one_sigkill(self):
        """os.kill(-1, signal.SIGKILL) → blocked."""
        content = "os.kill(-1, signal.SIGKILL)"
        result = _run_linter({"content": content, "file_path": "cleanup.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr
        assert "BLOCKED" in result.stderr

    def test_os_kill_minus_one_sigterm(self):
        """os.kill(-1, signal.SIGTERM) → blocked."""
        content = "os.kill(-1, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "proc.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_kill_zero_sigterm(self):
        """os.kill(0, signal.SIGTERM) → blocked."""
        content = "os.kill(0, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "signal.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_kill_zero_sigkill(self):
        """os.kill(0, signal.SIGKILL) → blocked."""
        content = "os.kill(0, signal.SIGKILL)"
        result = _run_linter({"content": content, "file_path": "killer.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_killpg_zero(self):
        """os.killpg(0, ...) → blocked."""
        content = "os.killpg(0, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "pgroup.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_killpg_one(self):
        """os.killpg(1, ...) → blocked (equivalent to kill ALL)."""
        content = "os.killpg(1, signal.SIGKILL)"
        result = _run_linter({"content": content, "file_path": "danger.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_kill_minus_one_with_spacing(self):
        """os.kill( -1, ...) with extra spacing → still blocked."""
        content = "os.kill( -1, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "spaced.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_os_killpg_one_with_spacing(self):
        """os.killpg( 1, ...) with extra spacing → still blocked."""
        content = "os.killpg( 1, signal.SIGKILL)"
        result = _run_linter({"content": content, "file_path": "spaced2.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_violation_in_new_string_field(self):
        """Kill pattern in new_string (Edit tool) → blocked."""
        result = _run_linter(
            {
                "new_string": "os.kill(-1, signal.SIGKILL)",
                "old_string": "pass",
                "file_path": "edit.py",
            }
        )
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_kill_literal_in_markdown_not_blocked(self):
        """applies_to: *.py — the kill literal quoted in a .md doc must NOT block."""
        content = "The audit flagged os.kill(-1, SIGKILL) as the dangerous call."
        result = _run_linter({"content": content, "file_path": "docs/audit.md"})
        assert result.returncode == 0, result.stderr
        assert "no-unguarded-kill" not in result.stderr


# ---------------------------------------------------------------------------
# Allow clean content (exit 0)
# ---------------------------------------------------------------------------


class TestAllowCleanContent:
    """Content without violations must produce exit 0."""

    def test_normal_python_code(self):
        """Plain Python with no anti-patterns → allowed."""
        content = "def process_data(items):\n    return [x for x in items if x.is_valid()]\n"
        result = _run_linter({"content": content, "file_path": "processor.py"})
        assert result.returncode == 0

    def test_display_none_without_error_keyword(self):
        """CSS display:none without error/unknown keywords → allowed."""
        content = ".sidebar { display: none; }"
        result = _run_linter({"content": content, "file_path": "style.css"})
        assert result.returncode == 0

    def test_xshow_without_negation(self):
        """Alpine x-show without negation of error → allowed."""
        content = '<div x-show="isReady">content</div>'
        result = _run_linter({"content": content, "file_path": "ready.html"})
        assert result.returncode == 0

    def test_showing_error_state_honestly(self):
        """Code that SHOWS error state (not hiding it) → allowed."""
        content = 'if status == "error":\n    render_error_banner(details)\n'
        result = _run_linter({"content": content, "file_path": "view.py"})
        assert result.returncode == 0

    def test_os_kill_with_valid_pid(self):
        """os.kill(pid, ...) with a variable (not -1 or 0) → allowed."""
        content = "os.kill(pid, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "cleanup.py"})
        assert result.returncode == 0

    def test_os_killpg_with_guarded_variable(self):
        """os.killpg(pgid, ...) with a variable → allowed."""
        content = "if pgid > 1:\n    os.killpg(pgid, signal.SIGTERM)\n"
        result = _run_linter({"content": content, "file_path": "guard.py"})
        assert result.returncode == 0

    def test_os_killpg_with_large_literal(self):
        """os.killpg(99999, ...) → allowed (not 0 or 1)."""
        content = "os.killpg(99999, signal.SIGTERM)"
        result = _run_linter({"content": content, "file_path": "test_proc.py"})
        assert result.returncode == 0

    def test_word_hide_without_error_context(self):
        """The word 'hide' without error/broken/unknown context → allowed."""
        content = "# hide the sidebar when viewport is narrow"
        result = _run_linter({"content": content, "file_path": "responsive.py"})
        assert result.returncode == 0

    def test_word_error_without_hide_context(self):
        """The word 'error' without hide/skip/suppress context → allowed."""
        content = "# log the error and raise to caller"
        result = _run_linter({"content": content, "file_path": "handler.py"})
        assert result.returncode == 0

    def test_word_suppress_without_error_context(self):
        """'suppress' without error/broken context → allowed."""
        content = "# suppress duplicate log entries"
        result = _run_linter({"content": content, "file_path": "logger.py"})
        assert result.returncode == 0

    def test_display_error_message(self):
        """Code that displays an error message → allowed."""
        content = "print(f'Error: {error_message}')"
        result = _run_linter({"content": content, "file_path": "output.py"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Escape hatch — behavioral-lint: ignore
# ---------------------------------------------------------------------------


class TestEscapeHatch:
    """Content with escape-hatch comments must be allowed despite violations."""

    def test_escape_unguarded_kill(self):
        """behavioral-lint: ignore no-unguarded-kill → allows kill(-1)."""
        content = "# behavioral-lint: ignore no-unguarded-kill\nos.kill(-1, signal.SIGTERM)\n"
        result = _run_linter({"content": content, "file_path": "escape.py"})
        assert result.returncode == 0

    def test_escape_hide_problems(self):
        """behavioral-lint: ignore no-hide-problems → allows the display:none block."""
        content = (
            "# behavioral-lint: ignore no-hide-problems\n"
            ".error-box { display: none; /* failed */ }\n"
        )
        result = _run_linter({"content": content, "file_path": "escape.css"})
        assert result.returncode == 0

    def test_escape_one_rule_still_blocks_other(self):
        """Escaping one rule doesn't escape a different rule."""
        content = "# behavioral-lint: ignore no-hide-problems\nos.kill(-1, signal.SIGKILL)\n"
        result = _run_linter({"content": content, "file_path": "partial.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr
        # Should NOT mention no-hide-problems since it's escaped
        assert "no-hide-problems" not in result.stderr

    def test_escape_both_rules(self):
        """Escaping both rules → allowed."""
        content = (
            "# behavioral-lint: ignore no-hide-problems\n"
            "# behavioral-lint: ignore no-unguarded-kill\n"
            ".error-box { display: none; /* failed */ }\n"
            "os.kill(-1, signal.SIGKILL)\n"
        )
        result = _run_linter({"content": content, "file_path": "all_escaped.py"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty content, parse failures, field selection."""

    def test_empty_content_allows(self):
        """Empty content field → exit 0."""
        result = _run_linter({"content": "", "file_path": "empty.py"})
        assert result.returncode == 0

    def test_no_content_field_allows(self):
        """No content or new_string field → exit 0."""
        result = _run_linter({"file_path": "delete.py"})
        assert result.returncode == 0

    def test_null_content_allows(self):
        """content: null → exit 0."""
        result = _run_linter({"content": None, "file_path": "null.py"})
        assert result.returncode == 0

    def test_json_parse_failure_allows(self):
        """Malformed JSON stdin → exit 0 (documented fail-open)."""
        result = _run_linter("this is not json at all {{{}}}}")
        assert result.returncode == 0
        assert "parse failed" in result.stderr.lower() or "WARNING" in result.stderr

    def test_empty_string_stdin_allows(self):
        """Empty string stdin → exit 0 (fail-open on parse error)."""
        result = _run_linter("")
        assert result.returncode == 0

    def test_content_field_used_for_write_tool(self):
        """Write tool sends content field — violations detected there."""
        result = _run_linter(
            {
                "content": "os.kill(-1, signal.SIGKILL)",
                "file_path": "write.py",
            }
        )
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_new_string_field_used_for_edit_tool(self):
        """Edit tool sends new_string field — violations detected there."""
        result = _run_linter(
            {
                "new_string": "os.killpg(1, signal.SIGTERM)",
                "old_string": "pass",
                "file_path": "edit.py",
            }
        )
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_old_string_not_checked(self):
        """old_string field should NOT be checked (it's existing code)."""
        result = _run_linter(
            {
                "old_string": "os.kill(-1, signal.SIGKILL)",
                "new_string": "os.kill(pid, signal.SIGTERM)",
                "file_path": "fix.py",
            }
        )
        assert result.returncode == 0

    def test_content_preferred_over_new_string(self):
        """When both content and new_string are present, content is checked.

        The script uses `data.get("content", "") or data.get("new_string", "")`,
        so if content is non-empty it takes precedence.
        """
        result = _run_linter(
            {
                "content": "normal safe code",
                "new_string": "os.kill(-1, signal.SIGKILL)",
                "file_path": "both.py",
            }
        )
        # content is clean → allowed (new_string not checked)
        assert result.returncode == 0

    def test_file_path_in_error_message(self):
        """The file_path appears in the violation message."""
        result = _run_linter(
            {
                "content": "os.kill(-1, signal.SIGTERM)",
                "file_path": "src/genesis/danger_zone.py",
            }
        )
        assert result.returncode == 2
        assert "src/genesis/danger_zone.py" in result.stderr


# ---------------------------------------------------------------------------
# Multiple rules — content that violates one but not the other
# ---------------------------------------------------------------------------


class TestMultipleRules:
    """Content that triggers one rule should not be confused with another."""

    def test_kill_violation_only(self):
        """Kill violation without hide violation → only no-unguarded-kill."""
        content = "os.kill(-1, signal.SIGKILL)"
        result = _run_linter({"content": content, "file_path": "kill_only.py"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr
        assert "no-hide-problems" not in result.stderr

    def test_hide_prose_warns_only(self):
        """Prose hide proposal without kill → warn (exit 0), only no-hide-problems."""
        content = "# hide the broken dashboard widget"
        result = _run_linter({"content": content, "file_path": "hide_only.py"})
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stderr
        assert "no-hide-problems" in result.stderr
        assert "no-unguarded-kill" not in result.stderr

    def test_prose_warn_and_kill_block(self):
        """Prose hide (warn) + kill (block) → exit 2, both reported.

        Verifies the warn pattern does not mask the block pattern (max-severity).
        """
        content = "# hide the broken data\nos.kill(-1, signal.SIGKILL)\n"
        result = _run_linter({"content": content, "file_path": "double.py"})
        assert result.returncode == 2
        assert "no-hide-problems" in result.stderr
        assert "no-unguarded-kill" in result.stderr


# ---------------------------------------------------------------------------
# Violation message structure
# ---------------------------------------------------------------------------


class TestViolationMessages:
    """Verify the structure of violation messages on stderr."""

    def test_blocked_message_structure(self):
        """Block messages include BLOCKED, rule name, file, issue, fix, escape."""
        content = "os.killpg(0, signal.SIGTERM)"
        result = _run_linter(
            {
                "content": content,
                "file_path": "src/process.py",
            }
        )
        assert result.returncode == 2
        stderr = result.stderr
        assert "BLOCKED" in stderr
        assert "no-unguarded-kill" in stderr
        assert "src/process.py" in stderr
        assert "Fix:" in stderr
        assert "Escape:" in stderr
        assert "behavioral-lint: ignore no-unguarded-kill" in stderr

    def test_warn_message_structure(self):
        """Prose warn messages use WARNING (not BLOCKED) and still carry Fix/Escape."""
        content = "# suppress the failed records"
        result = _run_linter(
            {
                "content": content,
                "file_path": "src/data.py",
            }
        )
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        assert "WARNING" in stderr
        assert "BLOCKED" not in stderr
        assert "no-hide-problems" in stderr
        assert "src/data.py" in stderr
        assert "behavioral-lint: ignore no-hide-problems" in stderr


# ---------------------------------------------------------------------------
# applies_to / excludes — the kill rule must fire on ALL code, not just *.py
# behavioral-lint: ignore no-unguarded-kill
# ---------------------------------------------------------------------------


class TestKillRuleFileScope:
    """no-unguarded-kill uses a docs exclude-list, not a *.py allow-list.

    Regression for the review finding: a `*.py` allow-list silently un-guarded
    the catastrophic call in extensionless CLI scripts, notebooks, and
    templates. The rule must fire on every code file and only skip doc formats.
    """

    _KILL = "os.kill(-1, signal.SIGKILL)"

    def test_extensionless_shebang_script_blocked(self):
        """A real target: scripts/gmodel is an extensionless python CLI."""
        result = _run_linter({"content": self._KILL, "file_path": "scripts/gmodel"})
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr

    def test_notebook_blocked(self):
        result = _run_linter({"content": self._KILL, "file_path": "explore.ipynb"})
        assert result.returncode == 2

    def test_template_blocked(self):
        result = _run_linter({"content": self._KILL, "file_path": "unit.service.j2"})
        assert result.returncode == 2

    def test_markdown_doc_allowed(self):
        result = _run_linter({"content": self._KILL, "file_path": "docs/audit.md"})
        assert result.returncode == 0, result.stderr
        assert "no-unguarded-kill" not in result.stderr

    def test_rst_doc_allowed(self):
        result = _run_linter({"content": self._KILL, "file_path": "docs/audit.rst"})
        assert result.returncode == 0, result.stderr

    def test_rules_dir_lookalike_path_still_linted(self):
        """The rules-dir skip must match the REAL dir, not any path containing
        the segment — a crafted src/.../config/behavioral_rules/ must still lint.
        """
        result = _run_linter(
            {"content": self._KILL, "file_path": "src/genesis/config/behavioral_rules/evil.py"}
        )
        assert result.returncode == 2
        assert "no-unguarded-kill" in result.stderr
