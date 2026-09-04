"""Advisory guard: a `tmux kill-server` with no explicit socket binding.

Origin (2026-09-04): a session's scratch-server cleanup ran a bare
`tmux kill-server` — the inherited $TMUX env var outranks TMUX_TMPDIR, so
the kill addressed the MAIN default server and reaped every live CC
session at once. The guard nudges toward binding the kill to its socket
(`-S <path>` / `-L <name>`) or clearing the inherited env first.

Advisory invariants under test: never blocks (exit 0 on every input),
silent on bound kills, silent on kill-session (legitimately used against
the default server by the slot launcher), silent on quoted mentions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "hooks"
    / "tmux_kill_server_guard.py"
)


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
    def test_bare_kill_server(self):
        """The origin case: an unbound kill-server aimed at a scratch server
        resolves via inherited $TMUX and takes down the main server."""
        advice = _advice("tmux kill-server")
        assert "kill-server" in advice
        assert advice.startswith("ADVISORY: ")

    def test_bare_kill_server_after_cleanup_chain(self):
        advice = _advice("rm -f /tmp/probe.log; tmux kill-server 2>/dev/null")
        assert "kill-server" in advice

    def test_nested_in_bash_c(self):
        advice = _advice("bash -c 'tmux kill-server'")
        assert "kill-server" in advice

    def test_env_wrapper_without_socket_still_fires(self):
        """TMUX_TMPDIR does NOT bind the target — $TMUX outranks it. Only an
        explicit -S/-L (or clearing TMUX) does; the wrapper alone still fires."""
        advice = _advice("TMUX_TMPDIR=/tmp/scratch tmux kill-server")
        assert "kill-server" in advice


class TestStaysSilent:
    def test_socket_path_bound(self):
        assert _advice("tmux -S /tmp/scratch/sock kill-server") == ""

    def test_socket_name_bound(self):
        assert _advice("tmux -L probe kill-server") == ""

    def test_glued_socket_forms(self):
        assert _advice("tmux -S/tmp/s.sock kill-server") == ""
        assert _advice("tmux -Lprobe kill-server") == ""

    def test_kill_session_unguarded(self):
        """The slot launcher legitimately kill-sessions on the default server."""
        assert _advice("tmux kill-session -t '=cc-3'") == ""

    def test_quoted_mention_not_execution(self):
        assert _advice("grep 'tmux kill-server' scripts/foo.sh") == ""

    def test_unrelated_commands(self):
        assert _advice("tmux ls") == ""
        assert _advice("echo tmux kill-server is dangerous") == ""

    def test_env_unset_tmux_is_the_remedy_not_a_repeat_offense(self):
        """Clearing the inherited $TMUX is exactly what the advisory asks
        for — the compliant retry must not draw the identical advisory."""
        assert _advice("env -u TMUX tmux kill-server") == ""
        assert _advice("env --unset TMUX tmux kill-server") == ""
        assert _advice("TMUX= tmux kill-server") == ""

    def test_tmux_assignment_to_a_value_still_fires(self):
        """Pointing $TMUX somewhere is a redirect, not a clearing — and
        TMUX_TMPDIR alone never overrides an inherited $TMUX."""
        advice = _advice("TMUX=/tmp/other/sock,1,0 tmux kill-server")
        assert "kill-server" in advice

    def test_untokenizable_stays_silent(self):
        """Degraded parse → silence: an advisory has no block to fail open
        from, and over-advising on unparseable text is pure noise."""
        assert _advice("tmux kill-server '") == ""


class TestNeverBlocks:
    def test_exit_zero_on_malformed_payload(self):
        r = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="not-json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0

    def test_exit_zero_on_missing_command(self):
        r = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps({"tool_input": {}}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0
