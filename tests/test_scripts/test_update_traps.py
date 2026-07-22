"""Trap-semantics locks for scripts/update.sh (deploy-audit P4b).

`set -Eeuo pipefail` extends the ERR trap into functions AND subshells, and a
SIGTERM/SIGINT handler now rolls back an interrupted deploy. Both are safety-
critical: a trap that fires from a subshell, or a signal handler that leaves the
server stopped, is worse than the bug it replaces. So this file has two layers:

  1. Extraction asserts against the REAL script text (structure/anchors).
  2. A FUNCTIONAL harness that sources the ACTUAL shipped `_on_err`,
     `_on_signal`, and `_on_signal_prestop` functions (extracted from
     update.sh) with a stubbed `_do_rollback`, and drives the four behaviors
     that matter:
       (a) an armed-window function failure rolls back,
       (b) a failing `$(...)` does NOT roll back from inside the subshell
           (rollback runs once, at BASH_SUBSHELL==0),
       (c) SIGTERM in the armed window rolls back,
       (d) SIGTERM in the pre-stop window cleans up WITHOUT rolling back.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def _extract_func(text: str, name: str) -> str:
    """Extract a `name() { ... }` definition (brace at column 0 closes it)."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, f"function {name} not found in update.sh"
    return f"{name}() {{\n{m.group(1)}\n}}"


# ── Extraction locks ───────────────────────────────────────────────────────
def test_errtrace_enabled(text: str) -> None:
    assert re.search(r"^set -Eeuo pipefail", text, re.MULTILINE), "need set -E (errtrace)"


def test_on_err_guards_subshell(text: str) -> None:
    body = _extract_func(text, "_on_err")
    assert "BASH_SUBSHELL" in body and 'exit "$exit_code"' in body, (
        "_on_err must short-circuit in a subshell so rollback never runs at depth>0"
    )


def test_signal_handlers_defined(text: str) -> None:
    assert "_on_signal() {" in text
    assert "_on_signal_prestop() {" in text


def test_prestop_trap_installed_before_stop(text: str) -> None:
    prestop = text.find("trap '_on_signal_prestop TERM' TERM")
    stop = text.find("--- Stopping services for update ---")
    armed = text.find("trap '_on_signal TERM' TERM")
    assert -1 < prestop < stop < armed, (
        "prestop signal trap must arm before the stop, armed one after"
    )


def test_armed_signal_trap_installed_with_err(text: str) -> None:
    err = text.find("trap _on_err ERR")
    sig = text.find("trap '_on_signal TERM' TERM")
    assert -1 < err < sig, "armed signal trap installs alongside the ERR trap"


def test_all_disarm_sites_clear_signals(text: str) -> None:
    # Every standalone ERR disarm must also drop INT/TERM so a post-disarm signal
    # doesn't fire a now-inappropriate handler. No bare `trap - ERR` may remain
    # (except inside a comment/backticks).
    for m in re.finditer(r"^\s*trap - ERR(.*)$", text, re.MULTILINE):
        tail = m.group(1)
        assert "INT TERM" in tail, (
            f"bare `trap - ERR` must become `trap - ERR INT TERM`: {m.group(0)!r}"
        )


# ── Functional harness (sources the real shipped functions) ────────────────
def _harness(text: str, scenario: str) -> str:
    """A self-contained script that reuses the SHIPPED trap functions with a
    stubbed _do_rollback (records BASH_SUBSHELL depth + reason to $RB_LOG) and
    stub state, then runs one scenario."""
    on_err = _extract_func(text, "_on_err")
    on_signal = _extract_func(text, "_on_signal")
    on_prestop = _extract_func(text, "_on_signal_prestop")
    return f"""#!/bin/bash
set -Eeuo pipefail
STATE_FILE="$STATE_FILE"
_do_rollback() {{ echo "depth=$BASH_SUBSHELL reason=$1" >> "$RB_LOG"; }}
{on_err}
{on_signal}
{on_prestop}

case "{scenario}" in
  subshell)
    trap _on_err ERR
    x=$(false)                 # failing command-sub under -E
    echo "PARENT_CONTINUED" >> "$RB_LOG"   # must NOT be reached
    ;;
  armed_fn)
    trap _on_err ERR
    _boom() {{ return 3; }}
    _boom                      # function failure in armed window
    echo "PARENT_CONTINUED" >> "$RB_LOG"
    ;;
  armed_sig)
    trap _on_err ERR
    trap '_on_signal INT' INT
    trap '_on_signal TERM' TERM
    echo READY > "$READY"
    sleep 30 & wait $!         # `wait` is interruptible so the trap fires now
    ;;
  prestop_sig)
    trap '_on_signal_prestop INT' INT
    trap '_on_signal_prestop TERM' TERM
    echo READY > "$READY"
    sleep 30 & wait $!
    ;;
esac
"""


def _run(tmp_path: Path, text: str, scenario: str, *, signal_it: bool = False):
    script = tmp_path / "harness.sh"
    script.write_text(_harness(text, scenario))
    rb_log = tmp_path / "rollback.log"
    state = tmp_path / "state.json"
    state.write_text("{}")
    ready = tmp_path / "ready"
    env = {
        **os.environ,
        "RB_LOG": str(rb_log),
        "STATE_FILE": str(state),
        "READY": str(ready),
    }
    if not signal_it:
        subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=15)
        return rb_log.read_text() if rb_log.exists() else "", state
    proc = subprocess.Popen(
        ["bash", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(100):  # wait for READY (trap installed, in sleep)
        if ready.exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)
    return (rb_log.read_text() if rb_log.exists() else ""), state


def test_armed_function_failure_rolls_back(tmp_path: Path, text: str) -> None:
    log, _ = _run(tmp_path, text, "armed_fn")
    assert "reason=" in log and "PARENT_CONTINUED" not in log
    assert log.count("depth=") == 1 and "depth=0" in log


def test_subshell_failure_does_not_rollback_in_subshell(tmp_path: Path, text: str) -> None:
    """The whole point of the -E guard: rollback runs ONCE, at depth 0, never
    from the subshell that actually failed."""
    log, _ = _run(tmp_path, text, "subshell")
    assert "PARENT_CONTINUED" not in log, "parent must not continue past a subshell failure"
    depths = re.findall(r"depth=(\d+)", log)
    assert depths == ["0"], f"rollback must run exactly once at depth 0, got {depths}"


def test_armed_sigterm_rolls_back(tmp_path: Path, text: str) -> None:
    log, _ = _run(tmp_path, text, "armed_sig", signal_it=True)
    assert "reason=update interrupted by SIGTERM" in log
    assert "depth=0" in log


def test_prestop_sigterm_cleans_up_without_rollback(tmp_path: Path, text: str) -> None:
    log, state = _run(tmp_path, text, "prestop_sig", signal_it=True)
    assert log == "", "pre-stop interrupt must NOT roll back (nothing merged yet)"
    assert not state.exists(), "pre-stop handler should remove the state file"


if sys.platform.startswith("win"):  # pragma: no cover
    pytest.skip("bash trap semantics are POSIX-only", allow_module_level=True)
