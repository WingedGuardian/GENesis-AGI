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


def test_old_collapsing_formula_is_gone(script_text):
    assert "ram_cap" not in script_text
    assert "PER_SESSION_MB=900" not in script_text
    assert "(avail_mb - RESERVED_MB)" not in script_text


def test_reattach_bypass_and_session_exists_preserved(script_text):
    assert 'tmux has-session -t "=$SESSION_NAME"' in script_text
    assert "_SESSION_EXISTS=1" in script_text


def test_fail_open_and_reclaim_present(script_text):
    assert "_cap_fail_open" in script_text
    assert "_cap_reclaim" in script_text
    assert "timeout 15" in script_text


def test_levers_sourced_and_exported_before_the_gate(script_text):
    # cc-slot.env must be sourced (and the cap levers exported) BEFORE the Python
    # gate call, or the documented tunables never take effect / never reach the
    # subprocess. Assert the source precedes the session_cap invocation.
    src_i = script_text.index('. "${HOME}/.genesis/cc-slot.env"')
    gate_i = script_text.index("-m genesis.cc.session_cap --existing")
    assert src_i < gate_i
    assert 'export "$_lever"' in script_text


# ── Hermetic end-to-end harness ───────────────────────────────────────────────
def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path, *, action, exists, session_names, rc):
    """Build a fake HOME + fake tmux + fake venv python. Returns (env, spawn_marker, kill_file).

    The fake tmux is FORMAT-AWARE: a names-only `-F` (the `existing` count) prints
    bare names; a pipe `-F` (the reclaim list) prints `name|0|t0`. session_names is
    a list of live cc-N names.
    """
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    venvbin = home / "genesis" / ".venv" / "bin"
    for d in (fakebin, venvbin, home / "genesis" / "scripts"):
        d.mkdir(parents=True, exist_ok=True)
    spawn_marker = tmp_path / "spawned"
    kill_file = tmp_path / "killed"
    names = "".join(f"{n}\n" for n in session_names)

    _write(
        fakebin / "tmux",
        f"""#!/bin/bash
sub=""; fmt=""; target=""
while [ $# -gt 0 ]; do
  case "$1" in
    has-session|list-sessions|kill-session|new-session) sub="$1" ;;
    -F) shift; fmt="${{1:-}}" ;;
    -t) shift; target="${{1:-}}" ;;
  esac
  shift
done
case "$sub" in
  has-session) exit {0 if exists else 1} ;;
  list-sessions)
    while IFS= read -r n; do
      [ -z "$n" ] && continue
      case "$fmt" in
        *"|"*) printf '%s|0|t0\\n' "$n" ;;
        *)     printf '%s\\n' "$n" ;;
      esac
    done <<< "$FAKE_NAMES"
    exit 0 ;;
  kill-session) printf '%s\\n' "$target" >> "{kill_file}"; exit 0 ;;
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
    }
    return env, spawn_marker, kill_file


def _run(
    tmp_path, *, action, exists=False, session_names=(), rc=0, mode="genesis-3-4", extra_env=None
):
    env, spawn_marker, kill_file = _setup(
        tmp_path, action=action, exists=exists, session_names=session_names, rc=rc
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(_CC_SLOT), mode], env=env, capture_output=True, text=True, timeout=30
    )
    killed = kill_file.read_text() if kill_file.exists() else ""
    return proc, spawn_marker.exists(), killed


def _run_pty(tmp_path, *, action, session_names, feed, exists=False, rc=0, mode="genesis-3-4"):
    """Run cc-slot.sh under a pty so the interactive reclaim `read </dev/tty` works."""
    import pty

    env, spawn_marker, kill_file = _setup(
        tmp_path, action=action, exists=exists, session_names=session_names, rc=rc
    )
    pid, fd = pty.fork()
    if pid == 0:  # child: becomes the pty session leader
        try:
            os.execve("/bin/bash", ["bash", str(_CC_SLOT), mode], env)  # noqa: S606 — pty child, fixed argv
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
    proc, spawned, _ = _run(
        tmp_path,
        action="DENY\nSession cap reached (4/4).",
        session_names=["cc-1", "cc-2", "cc-3", "cc-4"],
    )
    assert not spawned
    assert proc.returncode == 1
    assert "Session cap reached" in proc.stderr


def test_reattach_bypasses_gate_and_spawns(tmp_path):
    proc, spawned, _ = _run(
        tmp_path, action="DENY\nshould not be consulted", exists=True, session_names=_THREE
    )
    assert spawned, proc.stderr
    assert proc.returncode == 0
    assert "should not be consulted" not in proc.stderr


def test_python_unavailable_fails_open_and_spawns(tmp_path):
    proc, spawned, _ = _run(tmp_path, action="", rc=1, session_names=[])
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


def test_manual_door_failopen_over_cap_denies_no_reclaim(tmp_path):
    # Dashboard/"manual" door is the normal method held to the cap: even in
    # fail-open it must NOT get the destructive reclaim flow — a plain DENY.
    # A huge per-session lever forces SAFE_CAP to 1 deterministically.
    proc, spawned, killed = _run(
        tmp_path,
        action="",
        rc=1,
        mode="manual",
        session_names=["cc-1", "cc-2", "cc-3"],
        extra_env={"GENESIS_CC_PER_SESSION_MB": "1000000"},
    )
    assert not spawned
    assert proc.returncode == 1
    assert killed == ""
    assert "cap reached" in proc.stderr.lower()
