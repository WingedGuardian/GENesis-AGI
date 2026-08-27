"""Tests for scripts/hooks/pipe_status_guard.py.

Advises (never blocks) when `$?` is read after a pipeline, where it is the LAST
component's status rather than the command the author cares about.

The scope here is deliberately narrow. An earlier draft also flagged upper-case
variables a command read but never assigned, on the theory that they might be
pipeline state lost to a subshell. That was MEASURED as unusable — it fired on
19% of this repo's own documented piped commands (ordinary env vars, because the
Bash tool keeps a persistent shell across calls), it advised against
`${PIPESTATUS[0]}` which is the remedy this very hook recommends, and it went
SILENT on the careful form of the trap it targeted. Those cases are pinned below
so the branch cannot come back by accident.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pipe_status_guard.py"


def _run(command: str):
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _advice(command: str) -> str:
    """The advisory text for *command*, or "" when the guard stays silent."""
    r = _run(command)
    assert r.returncode == 0, f"advisory guard must never block: {r.stderr}"
    if not r.stdout.strip():
        return ""
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["additionalContext"]


class TestFiresOnTheRealTrap:
    def test_status_after_pipeline(self):
        """The origin case: reported exit 0 while the systemd unit had FAILED."""
        advice = _advice('systemd-run --user --wait /bin/bash x.sh | tail -2\necho "exit: $?"')
        assert "LAST component" in advice
        assert "PIPESTATUS" in advice, "the advice must name the remedy"

    def test_status_captured_into_a_variable(self):
        assert "LAST component" in _advice("foo | bar\nRC=$?")

    def test_fires_regardless_of_which_filter(self):
        assert _advice("prog | jq .x; echo $?") != ""


class TestSuppressedWhenTheAuthorAlreadyHandledIt:
    """Both of these were BLOCKERs in the first draft: the guard fired on the one
    case its own docstring called legitimate, and on its own recommended fix."""

    def test_pipefail_makes_status_correct(self):
        assert _advice("set -o pipefail; systemd-run --wait x | tail -2; echo $?") == ""

    def test_pipestatus_suppresses_even_alongside_a_status_read(self):
        """Genuinely falsifiable: `$?` IS present, so the command would advise
        were PIPESTATUS not suppressing it. Without the `$?` these commands would
        be silent for the trivial reason that nothing triggers at all."""
        assert _advice('foo | bar; echo "$? ${PIPESTATUS[0]}"') == ""
        assert _advice("prog | tail -2; RC=${PIPESTATUS[0]}; echo $?") == ""

    def test_pipestatus_alone_is_never_flagged(self):
        """Forward-guard against re-introducing the removed variable branch: that
        branch flagged `$PIPESTATUS` as an unassigned upper-case read and told the
        author to drop the pipe — advising against this hook's own remedy."""
        assert _advice('foo | bar; echo "${PIPESTATUS[0]}"') == ""
        assert _advice("systemd-run --wait x | tail -2; echo ${PIPESTATUS[0]}") == ""


class TestStaysSilent:
    """A noisy advisory gets muted, which costs more than the trap."""

    def test_no_pipe_at_all(self):
        assert _advice('echo hi; echo "$?"') == ""

    def test_no_status_read(self):
        assert _advice("ls -la | wc -l") == ""

    def test_or_operator_is_not_a_pipe(self):
        assert _advice('cmd && other || fallback; echo "$?"') == ""

    def test_pipe_inside_a_quoted_jq_program(self):
        assert _advice("gh api foo --jq '.[] | .name'; echo \"$?\"") == ""

    def test_literal_pipe_argument(self):
        assert _advice('grep -F "|" file; echo "$?"') == ""

    def test_command_substitution_captures_rather_than_pipes(self):
        """`$(cmd | filter)` is captured, so the outer `$?` is the assignment's.

        Deliberately includes a `$?` read: without it the command would be silent
        for the trivial reason that there is no `$?` at all, and the test could
        not fail even if the capture logic regressed.
        """
        assert _advice('RESULT=$(cmd | filter); echo "$?"') == ""

    def test_ordinary_environment_variables_never_trigger(self):
        """The measured 19%-false-positive class. These MUST stay silent — an
        env var read after a pipe is the normal case under a persistent shell."""
        for cmd in (
            'curl -sk -H "Authorization: $AUD" "$H/nodes/x/status" | jq .data',
            'git log --format="%h" | head; echo "$GIT_AUTHOR_NAME"',
            'curl -H "Authorization: bearer $GITHUB_TOKEN" x | jq .',
            'psql -c "select 1" | cat; echo "$PGHOST"',
        ):
            assert _advice(cmd) == "", f"false positive on: {cmd}"

    def test_shell_binders_never_trigger(self):
        """read / for / mapfile / printf -v bind variables the old heuristic
        could not see, and flagged as 'stale'."""
        for cmd in (
            'cat f | while read -r LINE; do echo "$LINE"; done',
            'for F in *.py; do echo "$F"; done | wc -l',
            'mapfile -t ARR < f; printf "%s\\n" "${ARR[@]}" | sort',
            'printf -v OUT "%s" x; echo "$OUT" | cat',
        ):
            assert _advice(cmd) == "", f"false positive on: {cmd}"


class TestNeverInterferes:
    def test_exit_zero_even_when_advising(self):
        r = _run('foo | bar\necho "$?"')
        assert r.returncode == 0
        assert r.stderr == ""

    def test_malformed_payload_is_silent(self):
        r = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="not json at all",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0 and r.stdout.strip() == ""

    def test_missing_command_is_silent(self):
        r = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps({"tool_input": {}}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0 and r.stdout.strip() == ""

    def test_non_string_command_is_silent(self):
        r = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps({"tool_input": {"command": ["a"]}}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0 and r.stdout.strip() == ""


class TestRegisteredAsAPreToolUseHook:
    def test_wired_into_settings(self):
        """Registration only — NOT proof of executability: .claude/hooks/genesis-hook
        resolves hook scripts against the MAIN worktree by design, so a hook added on
        a branch does not run until it merges."""
        settings = json.loads(
            (Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text()
        )
        commands = [
            h.get("command", "")
            for entry in settings["hooks"]["PreToolUse"]
            for h in entry.get("hooks", [])
        ]
        assert any("pipe_status_guard.py" in c for c in commands)
