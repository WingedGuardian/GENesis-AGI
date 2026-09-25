"""End-to-end guards for the cc-slot.sh capacity gate (session-cap redesign).

Static asserts that the gate is wired the safe way (delegates to
genesis.cc.session_cap, old collapsing free-RAM formula gone, reattach bypass +
_SESSION_EXISTS preserved, fail-open present, levers sourced before the gate),
PLUS a hermetic dynamic harness that runs the ACTUAL cc-slot.sh against fake
`tmux`/`python` — including a pty-driven pass over the interactive reclaim choice
(valid pick kills the right session and spawns; a leading-zero/octal input is
rejected, not silently spawned past the cap). No real sessions are created.
"""

from __future__ import annotations

import os
import select
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO / "scripts" / "cc-slot.sh"


# ── Static wiring guards ──────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def script_text() -> str:
    return _CC_SLOT.read_text()


def test_delegates_to_session_cap(script_text):
    assert "-m genesis.cc.session_cap --existing" in script_text


def _strip_comments(text: str) -> str:
    """Drop shell comments before matching call sites.

    The fix deliberately KEEPS the words `has-session` in prose explaining why
    it was wrong, so matching raw text would find the explanation and call it a
    defect.
    """
    import re as _re

    return "\n".join(_re.sub(r"#.*$", "", ln) for ln in text.splitlines())


def test_old_collapsing_formula_is_gone(script_text):
    assert "ram_cap" not in script_text
    assert "PER_SESSION_MB=900" not in script_text
    assert "(avail_mb - RESERVED_MB)" not in script_text


def test_reattach_bypass_and_session_exists_preserved(script_text):
    """The reattach bypass must still be gated on the session EXISTING.

    This pinned the spelling `tmux has-session -t "=$SESSION_NAME"`. That verb
    answers "absent" by ERRORING, and a tmux error is a server-side message
    logged in the server's message log -- on ordinary logins, since
    a brand new slot is absent every first time (#2140). The probe is now the
    silent `_session_exists`, so this asserts what the test was actually for:
    the bypass is still gated on an existence check of THIS session, and the
    noisy verb is gone.
    """
    assert "_SESSION_EXISTS=1" in script_text
    assert '_session_exists "$SESSION_NAME"' in script_text
    assert "tmux has-session" not in _strip_comments(script_text), (
        "cc-slot.sh probes with `tmux has-session`, which logs a server-side "
        "error on the absent path"
    )


def test_fail_open_and_reclaim_present(script_text):
    assert "_cap_fail_open" in script_text
    assert "_cap_reclaim" in script_text
    # Bounded AND with a hard kill deadline. Plain `timeout` sends TERM and then
    # WAITS, which a process wedged in uninterruptible I/O outlives — and this
    # gate can run after the consent rebuild already destroyed the slot, so a
    # TERM-only bound leaves the operator with neither pane nor replacement.
    assert 'timeout -k 2 15 "$_cap_py"' in script_text


def test_levers_sourced_and_exported_before_the_gate(script_text):
    # cc-slot.env must be sourced (and the cap levers exported) BEFORE the Python
    # gate call, or the documented tunables never take effect / never reach the
    # subprocess. The load lives in `_cc_load_levers` because the consent-rebuild
    # path re-runs it after the 120-second prompt; what matters here is that the
    # top-level CALL — not merely the definition — precedes the gate.
    call_i = script_text.index("\n_cc_load_levers\n")
    gate_i = script_text.index("-m genesis.cc.session_cap --existing")
    assert call_i < gate_i
    assert '. "${HOME}/.genesis/cc-slot.env"' in script_text
    assert 'export "$_l"' in script_text


def test_the_lever_loader_clears_before_it_reads(script_text):
    """A bare `.` only OVERLAYS, so a lever the operator DELETED survives.

    That matters because the consent prompt is precisely when they go and edit
    this file: removing `GENESIS_CC_PERMISSION_MODE=bypass` must disarm the
    rebuild, not leave the old value sitting in the shell.
    """
    body = script_text.split("_cc_load_levers() {", 1)[1].split("\n}", 1)[0]
    unset_i = body.index('unset "$_l"')
    src_i = body.index('. "${HOME}/.genesis/cc-slot.env"')
    assert unset_i < src_i, (
        "the loader must clear the known levers BEFORE sourcing, or a removed "
        "assignment can never take effect"
    )


# ── Hermetic end-to-end harness ───────────────────────────────────────────────
def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path, *, action, exists, session_names, rc, attached=""):
    """Build a fake HOME + fake tmux + fake venv python. Returns (env, spawn_marker, kill_file).

    The fake tmux is FORMAT-AWARE: a names-only `-F` (the `existing` count) prints
    bare names; a pipe `-F` (the reclaim list) prints `name|0|t0`. session_names is
    a list of live cc-N names.
    """
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    checkout = tmp_path / "checkout"
    scriptsbin = checkout / "scripts"
    venvbin = checkout / ".venv" / "bin"
    for d in (fakebin, scriptsbin, venvbin):
        d.mkdir(parents=True, exist_ok=True)
    script_path = scriptsbin / "cc-slot.sh"
    script_path.write_bytes(_CC_SLOT.read_bytes())
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    spawn_marker = tmp_path / "spawned"
    kill_file = tmp_path / "killed"
    names = "".join(f"{n}\n" for n in session_names)

    _write(
        fakebin / "tmux",
        f"""#!/bin/bash
sub=""; fmt=""; target=""; posfmt=""; filt=""
while [ $# -gt 0 ]; do
  case "$1" in
    has-session|list-sessions|kill-session|new-session|display-message) sub="$1" ;;
    -F) shift; fmt="${{1:-}}" ;;
    -f) shift; filt="${{1:-}}" ;;
    -t) shift; target="${{1:-}}" ;;
    -p) : ;;
    -*) : ;;
    *) posfmt="$1" ;;
  esac
  shift
done
case "$sub" in
  # `exists` survives only to drive this arm, and the door no longer calls
  # has-session at all -- test_cc_slot_silent_probe.py fails if it ever does
  # again. Existence for every live query is answered from FAKE_NAMES above.
  has-session) exit {0 if exists else 1} ;;   # legacy: the door no longer calls this
  display-message)
    # Per-name attach/activity query (display-message -p -t =cc-N of att|activity).
    nm="${{target#=}}"; att=0
    case " ${{FAKE_ATTACHED:-}} " in *" $nm "*) att=1 ;; esac
    printf '%s|t0\\n' "$att"
    exit 0 ;;
  list-sessions)
    case "$filt" in
      *"#{{==:#{{session_name}},"*)
        # ONE oracle: the name list. This branch used to short-circuit on
        # `exists` and report ANY queried name as present, which is the same
        # two-oracle split this fake was repaired to remove -- just pointing the
        # other way. It was latent rather than live (no test passed exists=True
        # with mode="manual"), and the failure it would have produced is nasty:
        # every slot reads present, so the free-slot loop never terminates, and
        # under _run_pty the waitpid after the select timeout has no timeout of
        # its own and hangs the suite. A test that wants a session present lists
        # it in session_names.
        want=$(printf '%s' "$filt" | sed 's/^#{{==:#{{session_name}},//; s/}}$//')
        while IFS= read -r n; do
          [ "$n" = "$want" ] && printf '%s\n' "$want"
        done <<< "$FAKE_NAMES"
        exit 0 ;;
    esac
    while IFS= read -r n; do
      [ -z "$n" ] && continue
      case "$fmt" in
        *"|"*)
          att=0
          case " ${{FAKE_ATTACHED:-}} " in *" $n "*) att=1 ;; esac
          printf '%s|%s|t0\\n' "$n" "$att" ;;
        *)     printf '%s\\n' "$n" ;;
      esac
    done <<< "$FAKE_NAMES"
    exit 0 ;;
  kill-session) if [ "${{KILL_RC:-0}}" = "0" ]; then printf '%s\\n' "$target" >> "{kill_file}"; fi; exit "${{KILL_RC:-0}}" ;;
  new-session) touch "{spawn_marker}"; exit 0 ;;
esac
exit 0
""",
    )
    _write(fakebin / "nproc", "#!/bin/bash\necho 6\n")
    _write(fakebin / "claude", "#!/bin/bash\nexit 0\n")
    _write(
        venvbin / "python",
        f"""#!/bin/bash
for a in "$@"; do
  case "$a" in
    *session_cap*) printf '%s\\n' "{action}"; exit {rc} ;;
    *login_gate*)  exit 1 ;;
  esac
done
exit 0
""",
    )
    env = {
        "HOME": str(home),
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "SSH_CONNECTION": "100.100.100.100 1 2 22",
        "FAKE_NAMES": names,
        "FAKE_ATTACHED": attached,
    }
    return env, spawn_marker, kill_file, script_path


def _run(
    tmp_path, *, action, exists=False, session_names=(), rc=0, mode="genesis-3-4", extra_env=None
):
    env, spawn_marker, kill_file, script_path = _setup(
        tmp_path, action=action, exists=exists, session_names=session_names, rc=rc
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(script_path), mode], env=env, capture_output=True, text=True, timeout=30
    )
    killed = kill_file.read_text() if kill_file.exists() else ""
    return proc, spawn_marker.exists(), killed


def _run_pty(
    tmp_path,
    *,
    action,
    session_names,
    feed,
    exists=False,
    rc=0,
    mode="genesis-3-4",
    kill_fails=False,
    attached="",
    extra_env=None,
):
    """Run cc-slot.sh under a pty so the interactive reclaim `read </dev/tty` works."""
    import pty

    env, spawn_marker, kill_file, script_path = _setup(
        tmp_path,
        action=action,
        exists=exists,
        session_names=session_names,
        rc=rc,
        attached=attached,
    )
    if kill_fails:
        env["KILL_RC"] = "1"
    if extra_env:
        env.update(extra_env)
    pid, fd = pty.fork()
    if pid == 0:  # child: becomes the pty session leader
        try:
            os.execve("/bin/bash", ["bash", str(script_path), mode], env)  # noqa: S606 — pty child, fixed argv
        except Exception:  # noqa: BLE001
            os._exit(127)
    os.write(fd, feed)
    out = b""
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 8)
        except OSError:
            break
        if not r:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    killed = kill_file.read_text() if kill_file.exists() else ""
    return code, out.decode(errors="replace"), spawn_marker.exists(), killed


_THREE = ["cc-1", "cc-2", "cc-3"]


def test_allow_spawns(tmp_path):
    proc, spawned, _ = _run(tmp_path, action="ALLOW\nSlot available (4/4).", session_names=_THREE)
    assert spawned, proc.stderr
    assert proc.returncode == 0


def test_deny_exits_without_spawning(tmp_path):
    # The slot being REQUESTED is cc-4 (mode genesis-3-4), so it must not also
    # appear in the session list: four sessions that exist, none of them this
    # one, is "at cap, asking for a new slot". The list previously contained
    # cc-4 while `exists` stayed False -- a state real tmux cannot produce, and
    # one that only passed because the fake answered existence from `exists`
    # and enumeration from the name list, two oracles that could disagree. With
    # a single oracle the contradiction surfaces, so the fixture is made
    # coherent rather than the oracle re-split.
    proc, spawned, _ = _run(
        tmp_path,
        action="DENY\nSession cap reached (4/4).",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5"],
    )
    assert not spawned
    assert proc.returncode == 1
    assert "Session cap reached" in proc.stderr


def test_reattach_bypasses_gate_and_spawns(tmp_path):
    # cc-4 is the slot the default mode targets, so listing it is what makes it
    # exist -- `exists=True` used to do that by fiat, from a second oracle.
    proc, spawned, _ = _run(
        tmp_path,
        action="DENY\nshould not be consulted",
        session_names=[*_THREE, "cc-4"],
    )
    assert spawned, proc.stderr
    assert proc.returncode == 0
    assert "should not be consulted" not in proc.stderr


def test_python_unavailable_fails_open_and_spawns(tmp_path):
    # Low per/floor so the "room for a full session" check passes regardless of
    # the CI runner's free RAM — we're testing the fail-open dispatch, not sizing.
    proc, spawned, _ = _run(
        tmp_path,
        action="",
        rc=1,
        session_names=[],
        extra_env={"GENESIS_CC_PER_SESSION_MB": "128", "GENESIS_CC_OOM_FLOOR_MB": "128"},
    )
    assert spawned, proc.stderr
    assert proc.returncode == 0
    assert "static fallback" in proc.stderr


def test_reclaim_without_tty_fails_safe_no_spawn(tmp_path):
    # RECLAIM with no interactive terminal (subprocess pipes) cannot prompt → fail
    # SAFE (guide to reattach, exit 1, spawn nothing), never silently over-spawn.
    proc, spawned, _ = _run(
        tmp_path,
        action="RECLAIM\nAt the emergency limit (5/4+1).\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
    )
    assert not spawned
    assert proc.returncode == 1
    assert "reattach" in proc.stderr.lower()


def test_reclaim_empty_oom_floor_refuses_no_spawn(tmp_path):
    # RECLAIM for reason oom_floor with NO cc session to reclaim → REFUSE (OOM
    # circuit-breaker), not a silent spawn on a swapless box.
    proc, spawned, _ = _run(
        tmp_path, action="RECLAIM\nRAM tight (900MB free).\noom_floor", session_names=[]
    )
    assert not spawned
    assert proc.returncode == 1
    assert "safety floor" in proc.stderr.lower()


def test_reclaim_empty_cap_full_proceeds(tmp_path):
    proc, spawned, _ = _run(tmp_path, action="RECLAIM\nAt the limit.\ncap_full", session_names=[])
    assert spawned, proc.stderr
    assert proc.returncode == 0
    assert "no cc-N session to reclaim" in proc.stderr


def test_reclaim_valid_choice_kills_selected_and_spawns(tmp_path):
    # pty-driven: pick "2" → ends the 2nd listed session (cc-2), then spawns.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit (5/4+1).\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"2\n",
    )
    assert spawned, out
    assert code == 0
    assert "cc-2" in killed  # exactly the chosen row's session
    assert "cc-1" not in killed


def test_reclaim_leading_zero_choice_rejected_no_spawn(tmp_path):
    # The security-review HIGH: "08" is octal in bash arithmetic. It must be
    # REJECTED (invalid selection, exit 1, nothing killed/spawned), never silently
    # unwind past the gate and spawn.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6", "cc-7", "cc-8", "cc-9"],
        feed=b"08\n",
    )
    assert not spawned
    assert code == 1
    assert "invalid selection" in out.lower()
    assert killed == ""


def test_reclaim_oversized_choice_rejected_no_wrong_kill(tmp_path):
    # Codex P2: an oversized decimal (2^64) slipped the regex, errored the bounds
    # check, and WRAPPED $((10#choice-1)) to -1 → killed the LAST session. Must be
    # rejected outright (invalid, exit 1, nothing killed/spawned).
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"18446744073709551616\n",
    )
    assert not spawned
    assert code == 1
    assert "invalid selection" in out.lower()
    assert killed == ""  # crucially: did NOT wrap-index and kill cc-6


def test_normal_method_no_ssh_failopen_over_cap_denies_not_reclaim(tmp_path):
    # F1: the "normal method" — no SSH_CONNECTION (dashboard web terminal / local
    # console) — is held to SAFE_CAP with a PLAIN DENY in fail-open, never the
    # destructive reclaim flow. safe = min((total-128)/128, nproc=6) = 6; 6 live
    # sessions → over cap. Small per/floor so `need` passes on any CI free RAM →
    # the reason is cap_full and the message is the count-cap one.
    proc, spawned, killed = _run(
        tmp_path,
        action="",
        rc=1,
        mode="manual",
        session_names=["cc-1", "cc-2", "cc-3", "cc-4", "cc-5", "cc-6"],
        extra_env={
            "SSH_CONNECTION": "",  # ← normal method: no SSH origin
            "GENESIS_CC_PER_SESSION_MB": "128",
            "GENESIS_CC_OOM_FLOOR_MB": "128",
        },
    )
    assert not spawned
    assert proc.returncode == 1
    assert killed == ""
    assert "session cap reached" in proc.stderr.lower()
    assert "capacity gate unavailable" in proc.stderr.lower()


def test_operator_ssh_failopen_over_cap_reclaims_not_denies(tmp_path):
    # F1 (the other half): an SSH login IS the operator regardless of door — over
    # the emergency limit in fail-open it gets the interactive RECLAIM, never a hard
    # deny. safe=6, emergency=1 → limit 7; 7 live sessions → over. pty feed "1" ends
    # cc-1 and spawns. Proves SSH presence (not the mode label) drives the affordance.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="",
        rc=1,
        mode="manual",
        session_names=["cc-1", "cc-2", "cc-3", "cc-4", "cc-5", "cc-6", "cc-7"],
        feed=b"1\n",
        extra_env={"GENESIS_CC_PER_SESSION_MB": "128", "GENESIS_CC_OOM_FLOOR_MB": "128"},
    )
    assert spawned, out
    assert code == 0
    assert "cc-1" in killed


def test_failopen_oversized_lever_no_divzero_abort(tmp_path):
    # F2: an oversized PER_SESSION lever (2^64) passed the old `^[1-9][0-9]*$` regex,
    # wrapped to 0 in bash $(( )), and div-by-zero ABORTED the login under set -e
    # (a silent lockout — the opposite of fail-open). It must now fall back to the
    # default and still produce a decision, never a bash arithmetic crash. Both the
    # allow and the reclaim paths carry "capacity gate unavailable"; only the crash
    # would emit "division by 0" with no decision.
    proc, spawned, killed = _run(
        tmp_path,
        action="",
        rc=1,
        session_names=[],
        extra_env={"GENESIS_CC_PER_SESSION_MB": "18446744073709551616"},
    )
    assert "division by 0" not in proc.stderr
    assert "capacity gate unavailable" in proc.stderr.lower()


def test_failopen_padded_lever_honored_matches_python(tmp_path):
    # Architect SHOULD-FIX: a whitespace-padded lever must be honored on the bash
    # fallback IDENTICALLY to the Python gate (which str.strip()s). A padded, huge
    # reserve forces SAFE_CAP=1; with 1 live session (no SSH → normal method) that
    # DENIES. If bash didn't strip, the padded value would be rejected → default
    # reserve (4096) → SAFE_CAP≈6 → the session would spawn. per/floor kept tiny so
    # the RAM gate passes on any CI free RAM, isolating the count axis.
    proc, spawned, killed = _run(
        tmp_path,
        action="",
        rc=1,
        mode="manual",
        session_names=["cc-1"],
        extra_env={
            "SSH_CONNECTION": "",  # normal method
            "GENESIS_CC_SYSTEM_RESERVE_MB": "  9999999  ",  # padded → strip → honored → SAFE_CAP=1
            "GENESIS_CC_PER_SESSION_MB": "128",
            "GENESIS_CC_OOM_FLOOR_MB": "128",
        },
    )
    assert not spawned  # padded reserve honored → cap 1 → over cap → deny
    assert proc.returncode == 1
    assert killed == ""
    assert "session cap reached" in proc.stderr.lower()


def test_reclaim_attached_victim_confirm_no_cancels(tmp_path):
    # F9: ending an ATTACHED session (someone may be using it) requires an explicit
    # y/N. Feeding the slot number then 'n' cancels — nothing killed, nothing spawned.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"1\nn\n",
        attached="cc-1",
    )
    assert not spawned
    assert code == 1
    assert killed == ""
    assert "attached" in out.lower()


def test_reclaim_attached_victim_confirm_yes_kills(tmp_path):
    # F9: the same attached victim, confirmed with 'y' → killed and spawned.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"1\ny\n",
        attached="cc-1",
    )
    assert spawned, out
    assert code == 0
    assert "cc-1" in killed


def test_reclaim_detached_victim_needs_no_confirm(tmp_path):
    # F9 boundary: a DETACHED victim is killed on the single number keystroke — no
    # second prompt. Feeding only "2\n" (no y/N) must still end cc-2 and spawn.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"2\n",
        attached="cc-1",  # cc-1 attached, but we pick cc-2 (detached)
    )
    assert spawned, out
    assert code == 0
    assert "cc-2" in killed


def test_reclaim_crafted_pipe_name_not_a_selectable_row(tmp_path):
    # Security HIGH (confused deputy): a crafted session whose NAME embeds '|'
    # (e.g. "cc-1|0|fake") must NOT enter the reclaim list. The old combined-line
    # split + prefix grep admitted it as a SECOND "cc-1" row with a forged att=0,
    # so picking it bypassed the attached-victim confirm and killed the REAL cc-1.
    # With the full-anchored name grep + per-name metadata query, only the real
    # cc-1 is listed, so selecting "2" is out of range — nothing killed or spawned.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit.\ncap_full",
        session_names=["cc-1", "cc-1|0|fake"],
        feed=b"2\n",
        attached="cc-1",
    )
    assert "invalid selection" in out.lower()  # crafted row is not selectable
    assert killed == ""  # the real cc-1 was NOT killed
    assert not spawned


def test_reclaim_kill_failure_aborts_no_spawn(tmp_path):
    # Codex P2: if `tmux kill-session` fails, NO memory was freed — must NOT spawn
    # a new session on top (that over-commits the box). pty-driven with kill_fails.
    code, out, spawned, killed = _run_pty(
        tmp_path,
        action="RECLAIM\nAt the emergency limit (5/4+1).\ncap_full",
        session_names=["cc-1", "cc-2", "cc-3", "cc-5", "cc-6"],
        feed=b"1\n",
        kill_fails=True,
    )
    assert not spawned
    assert code == 1
    assert killed == ""  # nothing recorded — the kill failed
    assert "failed to end" in out.lower()
