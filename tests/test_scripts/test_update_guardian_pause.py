"""Guardian pause/resume wiring in scripts/update.sh (PR-2).

A deploy must stand the host Guardian down across the genesis-server restart and
resume on EVERY exit, via the gateway pause/resume verbs (PR-1). This test locks
the invariants that make that safe — the three the design review flagged as
must-haves plus placement:

  - pause is invoked BEFORE the stop, gated on genesis-server being stopped;
  - resume rides the EXIT trap, COMPOSED with the temp-copy self-delete (never
    replacing it — a resume-only re-arm leaks the mktemp copy every deploy);
  - both SSH calls are best-effort (`|| true` + `timeout`): a failure must NOT
    abort the deploy, because at the pause site `set -e` is live and the ERR
    trap is not yet armed;
  - the new helpers use the column-0 brace convention so
    `test_update_traps.py::_extract_func` stays valid, and do not embed a literal
    `_stop_genesis_server\n` that would shift its anchor.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

# Exact strings (real statements, not comment prose).
SELF_DELETE = "trap 'rm -f \"${BASH_SOURCE[0]}\" 2>/dev/null' EXIT"
EXIT_REARM = "trap '_guardian_resume; rm -f \"${BASH_SOURCE[0]}\" 2>/dev/null' EXIT"
STOP_CALL = "\n    _stop_genesis_server\n"
PAUSE_CALL = "_guardian_pause"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def _extract_func(text: str, name: str) -> str:
    """Column-0-brace extraction, same shape as test_update_traps.py."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, f"function {name} not found with column-0 braces"
    return f"{name}() {{\n{m.group(1)}\n}}"


def _idx(text: str, marker: str) -> int:
    i = text.find(marker)
    assert i != -1, f"marker not found: {marker!r}"
    return i


# ── structure / placement ───────────────────────────────────────────────────
def test_helpers_defined_column0_braces(text: str) -> None:
    _extract_func(text, "_guardian_pause")
    _extract_func(text, "_guardian_resume")


def test_pause_called_before_the_stop(text: str) -> None:
    # the bare `_guardian_pause` call (defs are `_guardian_pause()`)
    call = re.search(r"^\s+_guardian_pause\n", text, re.MULTILINE)
    assert call, "no bare _guardian_pause call site"
    assert call.start() < _idx(text, STOP_CALL), "pause must be called before the stop"


def test_pause_is_host_gated(text: str) -> None:
    """The pause call sits inside the existing genesis-server WERE_RUNNING guard."""
    call = re.search(r"^\s+_guardian_pause\n", text, re.MULTILINE)
    assert call
    preceding = text[: call.start()]
    guard = preceding.rfind("genesis-server")
    assert guard != -1 and preceding.rfind("WERE_RUNNING") != -1


def test_resume_on_exit_trap_preserves_self_delete(text: str) -> None:
    """BLOCKER-2: the EXIT re-arm keeps the temp-copy self-delete."""
    assert EXIT_REARM in text, "EXIT trap must re-arm with resume AND the self-delete"
    assert text.count(SELF_DELETE) >= 1, "original self-delete EXIT trap must remain"


def test_resume_is_flag_guarded(text: str) -> None:
    body = _extract_func(text, "_guardian_resume")
    assert '"${_GUARDIAN_PAUSED:-}"' in body, "resume must no-op unless paused"


def test_ssh_calls_are_bounded_and_nonaborting(text: str) -> None:
    """BLOCKER-1: each SSH is bounded (timeout + ConnectTimeout) and cannot abort
    the deploy — both pause and resume guard the ssh with an `if`. The actual
    no-abort behaviour is proven functionally below."""
    for name in ("_guardian_pause", "_guardian_resume"):
        body = _extract_func(text, name)
        assert "ssh" in body, f"{name} must SSH the gateway"
        assert "timeout" in body and "ConnectTimeout" in body, f"{name} SSH must be bounded"
    # resume swallows a failed SSH: the ssh sits in an `if` CONDITION (set -e-safe,
    # so a non-zero exit can't abort) and the function returns 0 explicitly. The
    # `if` is load-bearing beyond swallowing — it clears _GUARDIAN_PAUSED only on
    # success, so a failed resume keeps the flag set for the EXIT-trap retry.
    resume = _extract_func(text, "_guardian_resume")
    assert re.search(r"if\s+timeout[^\n]*\bssh\b", resume), (
        "resume SSH must be guarded by an `if` (set -e-safe, non-aborting)"
    )
    assert "return 0" in resume, "resume must `return 0` so a failed resume never aborts"


def test_wire_contract_pause_int_in_gateway_range(text: str) -> None:
    r"""SHOULD-FIX #2: update.sh must send `pause <int>` with the int in the gateway's
    accepted 1-3600 range (PR-1's `pause\ *)` case). Pins our side of the cross-PR
    contract so a drift (TTL out of range, or a renamed verb) fails here."""
    pause = _extract_func(text, "_guardian_pause")
    assert '"pause $GUARDIAN_PAUSE_TTL"' in pause, "wire verb must be `pause <ttl>`"
    m = re.search(r"^GUARDIAN_PAUSE_TTL=(\d+)$", text, re.MULTILINE)
    assert m, "GUARDIAN_PAUSE_TTL must be a literal integer"
    assert 1 <= int(m.group(1)) <= 3600, "TTL must be in the gateway's 1-3600 range"


def test_syntax_ok(text: str) -> None:
    r = subprocess.run(["bash", "-n", str(UPDATE_SH)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


# ── functional: drive the SHIPPED helpers with a stubbed, LOGGED ssh ──────────
def _harness(text: str, tmp_path: Path, *, ssh_rc: int):
    """Fake host-config (yaml + venv-python + key) and a PATH ssh stub that LOGS its
    verb and exits ssh_rc. Returns (run(body) -> stdout+stderr, ssh_log Path)."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "guardian_remote.yaml").write_text("host_ip: 1.2.3.4\nhost_user: u\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "genesis_guardian_ed25519").write_text("")
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    # Delegate to real python3 so it actually parses the yaml (host_ip=1.2.3.4,
    # host_user=u, ssh_key ABSENT → '' → the helper falls back to the default key).
    # A fixed `echo` stub would return that string for EVERY parse — including the
    # ssh_key resolution — yielding a bogus key path that fails `[ -f "$key" ]`.
    (venv / "python").write_text('#!/bin/bash\nexec python3 "$@"\n')
    (venv / "python").chmod(0o755)
    stub = tmp_path / "bin"
    stub.mkdir()
    ssh_log = tmp_path / "ssh.log"
    (stub / "timeout").write_text('#!/bin/bash\nshift; exec "$@"\n')  # drop the N arg
    # log the LAST arg (the verb: "pause 1800" or "resume"), then exit ssh_rc
    (stub / "ssh").write_text(f'#!/bin/bash\necho "${{@: -1}}" >> "{ssh_log}"\nexit {ssh_rc}\n')
    for f in ("timeout", "ssh"):
        (stub / f).chmod(0o755)
    pause = _extract_func(text, "_guardian_pause")
    resume = _extract_func(text, "_guardian_resume")

    def run(body: str) -> str:
        script = tmp_path / "h.sh"
        script.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f'HOME="{home}"; VENV_DIR="{tmp_path}/venv"; GUARDIAN_PAUSE_TTL=1800\n'
            '_GUARDIAN_PAUSED=""; _GUARDIAN_HOST=""; _GUARDIAN_KEY=""\n'
            f"{pause}\n{resume}\n{body}\necho REACHED_END\n"
        )
        env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
        r = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=15
        )
        return r.stdout + r.stderr

    return run, ssh_log


def test_pause_success_arms_resume_and_sends_wire(text: str, tmp_path: Path) -> None:
    """Happy path: a SUCCEEDING pause sends `pause 1800`, sets the flag, and the EXIT
    trap fires `resume` on script exit (NOTE #3 + the live wire contract)."""
    run, ssh_log = _harness(text, tmp_path, ssh_rc=0)
    out = run('_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || true\n')
    assert "REACHED_END" in out and "PAUSED_SET" in out
    sent = ssh_log.read_text()
    assert "pause 1800" in sent, "must send the `pause <ttl>` wire verb"
    assert "resume" in sent, "the EXIT trap must fire resume on exit"


def test_pause_failure_proceeds_unpaused_no_abort(text: str, tmp_path: Path) -> None:
    """BLOCKER-1: a DENIED/unreachable pause (ssh rc!=0 — e.g. an OLD gateway) must
    NOT abort the deploy, must NOT mark paused, and must warn (not falsely 'paused')."""
    run, ssh_log = _harness(text, tmp_path, ssh_rc=255)
    out = run(
        '_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || echo UNPAUSED\n'
    )
    assert "REACHED_END" in out, "a failed pause must NOT abort the deploy"
    assert "UNPAUSED" in out and "PAUSED_SET" not in out, "a failed pause must not mark paused"
    assert "WARNING" in out, "a failed pause must warn, not misleadingly say paused"
    # never paused ⇒ EXIT resume never fires (no spurious resume on the wire)
    assert "resume" not in (ssh_log.read_text() if ssh_log.exists() else "")


def test_resume_best_effort_on_ssh_failure(text: str, tmp_path: Path) -> None:
    run, _ = _harness(text, tmp_path, ssh_rc=255)
    out = run(
        '_GUARDIAN_PAUSED=1; _GUARDIAN_HOST="u@1.2.3.4"; _GUARDIAN_KEY="/x"\n_guardian_resume\n'
    )
    assert "REACHED_END" in out, "a failed resume SSH must NOT abort"


def test_resume_keeps_flag_set_on_failure_clears_on_success(text: str, tmp_path: Path) -> None:
    """P2 #3: the flag is cleared ONLY after the gateway accepts `resume`. A FAILED
    resume keeps _GUARDIAN_PAUSED set so the EXIT-trap retries (else a transient SSH
    failure leaves the host paused until its TTL); a SUCCESSFUL resume clears it."""
    probe = (
        '_GUARDIAN_PAUSED=1; _GUARDIAN_HOST="u@1.2.3.4"; _GUARDIAN_KEY="/x"\n'
        "_guardian_resume\n"
        '[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo FLAG_KEPT || echo FLAG_CLEARED\n'
    )
    run_fail, _ = _harness(text, tmp_path / "fail", ssh_rc=255)
    assert "FLAG_KEPT" in run_fail(probe), "a FAILED resume must keep the flag for the EXIT retry"
    run_ok, _ = _harness(text, tmp_path / "ok", ssh_rc=0)
    assert "FLAG_CLEARED" in run_ok(probe), "a SUCCESSFUL resume must clear the flag (idempotent)"


def test_resume_noops_when_not_paused(text: str, tmp_path: Path) -> None:
    """Guard-the-guard: resume must NOT SSH when _GUARDIAN_PAUSED is unset."""
    run, ssh_log = _harness(text, tmp_path, ssh_rc=255)
    out = run("_guardian_resume\n")  # harness leaves _GUARDIAN_PAUSED=""
    assert "REACHED_END" in out
    assert not ssh_log.exists() or "resume" not in ssh_log.read_text(), "no spurious resume"


if sys.platform.startswith("win"):  # pragma: no cover
    pytest.skip("bash-only", allow_module_level=True)
