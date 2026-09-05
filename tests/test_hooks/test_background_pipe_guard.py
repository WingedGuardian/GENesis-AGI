"""Tests for scripts/hooks/background_pipe_guard.py.

Blocks a run_in_background Bash command with a REAL top-level pipe (its stdout is
swallowed → empty output). The prior inline check (`${CMD//||/ }` then
`grep -qF "|"`) over-blocked on a `|` inside a quoted jq program / `grep -F '|'`
/ a `||`; this hook uses the quote/redirect-aware shell_parse.has_top_level_pipe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "background_pipe_guard.py"


def _run(command, *, background):
    payload = json.dumps({"tool_input": {"command": command, "run_in_background": background}})
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_background_real_pipe_blocked():
    r = _run("cat x | grep y", background=True)
    assert r.returncode == 2
    assert "BLOCKED" in r.stderr


def test_background_pipe_both_streams_blocked():
    r = _run("make |& tee log", background=True)
    assert r.returncode == 2


def test_background_quoted_jq_pipe_allowed():
    # The core false-positive fix: a `|` inside a quoted jq program is NOT a pipe.
    # (The old inline `grep -qF "|"` blocked this.)
    r = _run("gh api foo --jq '.[] | .name' > out.json", background=True)
    assert r.returncode == 0, r.stderr


def test_background_quoted_pipe_only_allowed():
    r = _run("grep -F '|' file > out", background=True)
    assert r.returncode == 0, r.stderr


def test_background_logical_or_allowed():
    r = _run("grep -q foo file || echo none", background=True)
    assert r.returncode == 0, r.stderr


def test_background_redirect_clobber_allowed():
    r = _run("git 2>| err.log push origin main", background=True)
    assert r.returncode == 0, r.stderr


def test_background_no_pipe_allowed():
    r = _run("ls -la ~/tmp", background=True)
    assert r.returncode == 0


def test_foreground_pipe_allowed():
    # The empty-output trap is background-only.
    r = _run("cat x | grep y", background=False)
    assert r.returncode == 0


def test_background_as_string_true_blocked():
    # run_in_background may arrive as the string "true".
    payload = json.dumps({"tool_input": {"command": "a | b", "run_in_background": "true"}})
    r = subprocess.run(
        [sys.executable, str(_HOOK)], input=payload, capture_output=True, text=True, timeout=10
    )
    assert r.returncode == 2


def test_empty_payload_fail_open():
    r = subprocess.run(
        [sys.executable, str(_HOOK)], input="", capture_output=True, text=True, timeout=10
    )
    assert r.returncode == 0


def test_a_refusal_names_the_collateral_it_discarded():
    """This exit discards the WHOLE call, and the block message names only the pipe.

    `cp a b && producer | consumer` loses the copy too, but the refusal reads as
    "the pipeline was rejected" — never "and your earlier write never happened".
    Every other refusal point in this repo emits the discarded-write note; this
    guard was wired without it, so its refusals were the silent ones.
    """
    r = _run("cp a b && producer | consumer", background=True)
    assert r.returncode == 2, r.stderr
    assert "BLOCKED" in r.stderr
    assert "ENTIRE command was discarded" in r.stderr, (
        "the refusal did not say the earlier steps were lost; "
        f"stderr={r.stderr!r}"
    )


def test_a_pure_pipeline_still_gets_the_note():
    """Documents actual, shared behaviour rather than what I first assumed.

    MEASURED: `split_segments("cat x | grep y")` returns 2, so the helper counts
    a pipeline's two sides as two steps and the note fires. That is the SHARED
    helper's behaviour at every refusal point in this repo, not something this
    guard does differently — pinning it here keeps this guard consistent with
    the others rather than silently diverging.

    Whether a pipeline SHOULD count as one step is a separate question about the
    helper (a pipe is a single foreground job; `&&`/`;` genuinely chain), and it
    is raised on the PR rather than changed here — it would alter the note at
    every guard, which is well outside this one's blast radius.
    """
    r = _run("cat x | grep y", background=True)
    assert r.returncode == 2
    assert "ENTIRE command was discarded" in r.stderr


def test_the_note_never_changes_the_verdict():
    """Cosmetic means cosmetic: an allowed command stays allowed, silently."""
    r = _run("cp a b && cat x", background=True)
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", f"an allowed command emitted output: {r.stderr!r}"
