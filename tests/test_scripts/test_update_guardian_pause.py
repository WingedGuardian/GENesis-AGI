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
  - the helpers use the column-0 brace convention so `_extract_func` stays valid,
    and do not embed a literal `_stop_genesis_server\n` that would shift its anchor.

SINCE #1699 the helper BODIES live in `scripts/lib/guardian_pause.sh` (shared with
the code-only deploy wrapper), so body-level assertions read `lib_text` while the
call site, its placement, the EXIT-trap composition and the SOURCE wiring are still
asserted against update.sh's own text.
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
# The pause/resume/renew trio moved VERBATIM out of update.sh into this lib
# (issue #1699) so the code-only deploy wrapper shares it instead of growing a
# replica. Every invariant below still holds — it is now split across two
# files: the FUNCTION BODIES live in the lib, while the CALL SITE, its
# placement before the stop, and the EXIT-trap composition (which update.sh
# owns, because it composes resume with its temp-copy self-delete) stay here.
GUARDIAN_LIB = REPO_ROOT / "scripts" / "lib" / "guardian_pause.sh"

# Exact strings (real statements, not comment prose).
SELF_DELETE = "trap 'rm -f \"${BASH_SOURCE[0]}\" 2>/dev/null' EXIT"
EXIT_REARM = "trap '_guardian_resume; rm -f \"${BASH_SOURCE[0]}\" 2>/dev/null' EXIT"
STOP_CALL = "\n    _stop_genesis_server\n"
PAUSE_CALL = "_guardian_pause"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


@pytest.fixture(scope="module")
def lib_text() -> str:
    """scripts/lib/guardian_pause.sh — where the helper BODIES live since #1699."""
    return GUARDIAN_LIB.read_text()


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
def test_helpers_defined_column0_braces(lib_text: str) -> None:
    _extract_func(lib_text, "_guardian_pause")
    _extract_func(lib_text, "_guardian_resume")


def test_update_sh_sources_the_guardian_lib(text: str) -> None:
    """The extraction is only real if update.sh actually LOADS the lib — otherwise
    every helper call below is an undefined-command error at deploy time. Pins the
    wiring the move created (built != wired)."""
    assert 'source "$SCRIPT_DIR/lib/guardian_pause.sh"' in text, (
        "update.sh must source the guardian-pause lib that now holds its helpers"
    )
    src = _idx(text, 'source "$SCRIPT_DIR/lib/guardian_pause.sh"')
    call = re.search(r"^\s+_guardian_pause\n", text, re.MULTILINE)
    assert call and src < call.start(), "the lib must be sourced BEFORE the pause call"


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


def test_resume_is_flag_guarded(lib_text: str) -> None:
    body = _extract_func(lib_text, "_guardian_resume")
    assert '"${_GUARDIAN_PAUSED:-}"' in body, "resume must no-op unless paused"


def test_ssh_calls_are_bounded_and_nonaborting(lib_text: str) -> None:
    """BLOCKER-1: each SSH is bounded (timeout + ConnectTimeout) and cannot abort
    the deploy — both pause and resume guard the ssh with an `if`. The actual
    no-abort behaviour is proven functionally below."""
    for name in ("_guardian_pause", "_guardian_resume"):
        body = _extract_func(lib_text, name)
        assert "ssh" in body, f"{name} must SSH the gateway"
        assert "timeout" in body and "ConnectTimeout" in body, f"{name} SSH must be bounded"
    # resume swallows a failed SSH: the ssh sits in an `if` CONDITION (set -e-safe,
    # so a non-zero exit can't abort) and the function returns 0 explicitly. The
    # `if` is load-bearing beyond swallowing — it clears _GUARDIAN_PAUSED only on
    # success, so a failed resume keeps the flag set for the EXIT-trap retry.
    resume = _extract_func(lib_text, "_guardian_resume")
    assert re.search(r"if\s+timeout[^\n]*\bssh\b", resume), (
        "resume SSH must be guarded by an `if` (set -e-safe, non-aborting)"
    )
    assert "return 0" in resume, "resume must `return 0` so a failed resume never aborts"


def test_wire_contract_pause_int_in_gateway_range(lib_text: str) -> None:
    r"""SHOULD-FIX #2: the wire verb is `pause <int>` with the int in the gateway's
    accepted 1-3600 range (PR-1's `pause\ *)` case). Pins our side of the cross-PR
    contract so a drift (TTL out of range, or a renamed verb) fails here.

    Since #1699 the TTL is a lib DEFAULT a caller may override before sourcing, so
    EVERY value that can reach the wire is checked: the lib's `:=` default and each
    caller's literal override (deploy_code_only.sh uses a shorter window)."""
    pause = _extract_func(lib_text, "_guardian_pause")
    assert '"pause $GUARDIAN_PAUSE_TTL"' in pause, "wire verb must be `pause <ttl>`"
    m = re.search(r'^: "\$\{GUARDIAN_PAUSE_TTL:=(\d+)\}"$', lib_text, re.MULTILINE)
    assert m, "the lib must define GUARDIAN_PAUSE_TTL as a literal-integer := default"
    ttls = [int(m.group(1))]
    for caller in ("deploy_code_only.sh",):
        ctext = (REPO_ROOT / "scripts" / caller).read_text()
        ttls += [int(v) for v in re.findall(r"^GUARDIAN_PAUSE_TTL=(\d+)$", ctext, re.MULTILINE)]
    for ttl in ttls:
        assert 1 <= ttl <= 3600, f"TTL {ttl} outside the gateway's 1-3600 range"


@pytest.mark.parametrize(
    "script", [UPDATE_SH, GUARDIAN_LIB], ids=["update.sh", "guardian_pause.sh"]
)
def test_syntax_ok(script: Path) -> None:
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


# ── functional: drive the SHIPPED helpers with a stubbed, LOGGED ssh ──────────
def _harness(lib_text: str, tmp_path: Path, *, ssh_rc: int, pre_paused: bool = False):
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
    # log the LAST arg (the verb) always; the `paused` query additionally returns
    # its JSON on stdout (the ownership check greps it), so pre_paused drives it.
    _paused_json = '{"paused": true}' if pre_paused else '{"paused": false}'
    (stub / "ssh").write_text(
        "#!/bin/bash\n"
        'verb="${@: -1}"\n'
        f'echo "$verb" >> "{ssh_log}"\n'
        f"if [ \"$verb\" = paused ]; then echo '{_paused_json}'; fi\n"
        f"exit {ssh_rc}\n"
    )
    for f in ("timeout", "ssh"):
        (stub / f).chmod(0o755)
    pause = _extract_func(lib_text, "_guardian_pause")
    resume = _extract_func(lib_text, "_guardian_resume")
    renew = _extract_func(lib_text, "_guardian_renew_loop")

    def run(body: str) -> str:
        script = tmp_path / "h.sh"
        script.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f'HOME="{home}"; VENV_DIR="{tmp_path}/venv"; GUARDIAN_PAUSE_TTL=1800\n'
            "GUARDIAN_PAUSE_RENEW_MAX=4\n"
            '_GUARDIAN_PAUSED=""; _GUARDIAN_HOST=""; _GUARDIAN_KEY=""; _GUARDIAN_RENEW_PID=""\n'
            f"{pause}\n{resume}\n{renew}\n{body}\necho REACHED_END\n"
        )
        env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
        r = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=15
        )
        return r.stdout + r.stderr

    return run, ssh_log


def test_pause_success_sends_wire_and_sets_flag(lib_text: str, tmp_path: Path) -> None:
    """Happy path: a SUCCEEDING pause sends `pause 1800` and sets the flag.

    The EXIT-trap half of the old assertion moved to
    ``test_every_caller_arms_a_resume_exit_trap`` + the composed test below: since
    #1699 the lib does NOT arm the trap (each caller composes resume with its own
    cleanup — update.sh with its temp-copy self-delete, the wrapper with its lock
    and state teardown), so asserting it against a lib-only harness would be
    asserting a property this file no longer owns."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=0)
    out = run('_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || true\n')
    assert "REACHED_END" in out and "PAUSED_SET" in out
    sent = ssh_log.read_text()
    assert "pause 1800" in sent, "must send the `pause <ttl>` wire verb"


def test_resume_fires_on_exit_when_the_caller_arms_the_trap(lib_text: str, tmp_path: Path) -> None:
    """The composed behaviour the old test covered, with the caller's half supplied
    explicitly: arm the trap the way every real caller does, and `resume` must reach
    the wire on exit. Guards the lib side — a resume that no longer SSHes, or one
    that returns early after a successful pause, fails here."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=0)
    run("trap '_guardian_resume' EXIT\n_guardian_pause\n")
    assert "resume" in ssh_log.read_text(), "resume must reach the gateway on exit"


@pytest.mark.parametrize(
    "caller,trap_line,resume_site",
    [
        # update.sh arms it inline at the call site, composed with the self-delete.
        ("update.sh", EXIT_REARM, EXIT_REARM),
        # the wrapper arms a cleanup function that calls resume.
        ("deploy_code_only.sh", "trap _cleanup EXIT", "_guardian_resume"),
    ],
)
def test_every_caller_arms_a_resume_exit_trap(
    caller: str, trap_line: str, resume_site: str
) -> None:
    """The invariant the lib gave up when the trap moved out: a caller that pauses the
    guardian MUST arm an EXIT trap that resumes it. Without this, a new caller could
    source the lib, pause, and leave the host guardian stood down until its TTL —
    exactly the failure the hard TTL exists to bound but should never have to."""
    ctext = (REPO_ROOT / "scripts" / caller).read_text()
    assert "_guardian_pause" in ctext, f"{caller} should call _guardian_pause"
    assert trap_line in ctext, f"{caller} must arm an EXIT trap ({trap_line!r})"
    assert resume_site in ctext, f"{caller}'s EXIT path must reach _guardian_resume"


def test_pause_failure_proceeds_unpaused_no_abort(lib_text: str, tmp_path: Path) -> None:
    """BLOCKER-1: a DENIED/unreachable pause (ssh rc!=0 — e.g. an OLD gateway) must
    NOT abort the deploy, must NOT mark paused, and must warn (not falsely 'paused')."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=255)
    out = run(
        '_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || echo UNPAUSED\n'
    )
    assert "REACHED_END" in out, "a failed pause must NOT abort the deploy"
    assert "UNPAUSED" in out and "PAUSED_SET" not in out, "a failed pause must not mark paused"
    assert "WARNING" in out, "a failed pause must warn, not misleadingly say paused"
    # never paused ⇒ EXIT resume never fires (no spurious resume on the wire)
    assert "resume" not in (ssh_log.read_text() if ssh_log.exists() else "")


def test_pause_skips_when_already_paused(lib_text: str, tmp_path: Path) -> None:
    """P2 #1: if the gateway already has an UNEXPIRED pause (operator/another
    workflow), _guardian_pause must NOT re-pause and must NOT arm resume — so our
    EXIT never removes a pause we did not create."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=0, pre_paused=True)
    out = run(
        '_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || echo UNPAUSED\n'
    )
    assert "REACHED_END" in out
    assert "UNPAUSED" in out and "PAUSED_SET" not in out, "must not own a pre-existing pause"
    assert "already paused" in out
    sent = ssh_log.read_text() if ssh_log.exists() else ""
    assert "paused" in sent, "must query the gateway pause state"
    assert "pause 1800" not in sent, "must NOT send our own pause over a pre-existing one"


def test_pause_proceeds_when_not_already_paused(lib_text: str, tmp_path: Path) -> None:
    """P2 #1 (other side): with no pre-existing pause, _guardian_pause queries then
    pauses and arms resume as normal."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=0, pre_paused=False)
    out = run(
        '_guardian_pause\n[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo PAUSED_SET || echo UNPAUSED\n'
    )
    assert "PAUSED_SET" in out
    sent = ssh_log.read_text()
    assert "paused" in sent and "pause 1800" in sent, "must query THEN pause"


def test_resume_best_effort_on_ssh_failure(lib_text: str, tmp_path: Path) -> None:
    run, _ = _harness(lib_text, tmp_path, ssh_rc=255)
    out = run(
        '_GUARDIAN_PAUSED=1; _GUARDIAN_HOST="u@1.2.3.4"; _GUARDIAN_KEY="/x"\n_guardian_resume\n'
    )
    assert "REACHED_END" in out, "a failed resume SSH must NOT abort"


def test_resume_keeps_flag_set_on_failure_clears_on_success(lib_text: str, tmp_path: Path) -> None:
    """P2 #3: the flag is cleared ONLY after the gateway accepts `resume`. A FAILED
    resume keeps _GUARDIAN_PAUSED set so the EXIT-trap retries (else a transient SSH
    failure leaves the host paused until its TTL); a SUCCESSFUL resume clears it."""
    probe = (
        '_GUARDIAN_PAUSED=1; _GUARDIAN_HOST="u@1.2.3.4"; _GUARDIAN_KEY="/x"\n'
        "_guardian_resume\n"
        '[ "${_GUARDIAN_PAUSED:-}" = 1 ] && echo FLAG_KEPT || echo FLAG_CLEARED\n'
    )
    run_fail, _ = _harness(lib_text, tmp_path / "fail", ssh_rc=255)
    assert "FLAG_KEPT" in run_fail(probe), "a FAILED resume must keep the flag for the EXIT retry"
    run_ok, _ = _harness(lib_text, tmp_path / "ok", ssh_rc=0)
    assert "FLAG_CLEARED" in run_ok(probe), "a SUCCESSFUL resume must clear the flag (idempotent)"


def test_resume_noops_when_not_paused(lib_text: str, tmp_path: Path) -> None:
    """Guard-the-guard: resume must NOT SSH when _GUARDIAN_PAUSED is unset."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=255)
    out = run("_guardian_resume\n")  # harness leaves _GUARDIAN_PAUSED=""
    assert "REACHED_END" in out
    assert not ssh_log.exists() or "resume" not in ssh_log.read_text(), "no spurious resume"


def test_lease_renewer_wired_and_bounded(lib_text: str) -> None:
    """P2 #4: the renewer is bounded (GUARDIAN_PAUSE_RENEW_MAX), started as a
    REDIRECTED background job by _guardian_pause, and killed by _guardian_resume
    BEFORE the resume SSH (so it can't re-pause after we resume)."""
    renew = _extract_func(lib_text, "_guardian_renew_loop")
    assert "GUARDIAN_PAUSE_RENEW_MAX" in renew, "renewer must be bounded (no runaway)"
    assert "pause $GUARDIAN_PAUSE_TTL" in renew, "renewer must re-issue the pause verb"
    pause = _extract_func(lib_text, "_guardian_pause")
    assert "_guardian_renew_loop >/dev/null 2>&1 &" in pause, (
        "pause starts the renewer (redirected bg)"
    )
    assert "_GUARDIAN_RENEW_PID=$!" in pause, "pause must capture the renewer PID"
    resume = _extract_func(lib_text, "_guardian_resume")
    assert 'kill "$_GUARDIAN_RENEW_PID"' in resume, "resume must kill the renewer"
    assert resume.index("_GUARDIAN_RENEW_PID") < resume.index("resume >/dev/null"), (
        "the renewer must be killed BEFORE the resume SSH"
    )


def test_lease_renewer_reissues_pause_then_stops(lib_text: str, tmp_path: Path) -> None:
    """P2 #4 functional: with a short TTL the renewer re-issues `pause` at least once
    (the lease is renewed), and _guardian_resume stops it (no runaway re-pausing)."""
    run, ssh_log = _harness(lib_text, tmp_path, ssh_rc=0)
    # TTL=2 → renewer sleeps 1s; keep the script alive ~3s to observe >=1 renewal,
    # then _guardian_resume kills it.
    run("GUARDIAN_PAUSE_TTL=2\n_guardian_pause\nsleep 3\n_guardian_resume\n")
    sent = ssh_log.read_text() if ssh_log.exists() else ""
    n = sent.count("pause 2")
    assert n >= 2, f"renewer must re-issue pause at least once (saw {n} 'pause 2')"


if sys.platform.startswith("win"):  # pragma: no cover
    pytest.skip("bash-only", allow_module_level=True)
