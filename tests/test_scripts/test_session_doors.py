"""Session doors: cc-slot.sh manual mode + the bootstrap bashrc wrapper.

WS-D2 (2026-07-16): every interactive door — SSH slot hostnames, manual SSH,
the dashboard web terminal — converges on cc-slot.sh's attach-or-create tmux
slots, replacing the uniquely-named `cc-manual-<ts>-<pid>` wrapper whose
never-reattach design manufactured orphans (and the duplicate-session guard
that papered over it, removed in the same change).

Behavioral tests run the real script against a fake `tmux` on PATH that
records its argv, so slot allocation, exact-name matching, arg forwarding,
and permission-flag suppression are exercised end-to-end rather than
asserted as text.
"""

from __future__ import annotations

import contextlib
import os
import pty
import select
import stat
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO_ROOT / "scripts" / "cc-slot.sh"
_BOOTSTRAP = _REPO_ROOT / "scripts" / "bootstrap.sh"

_FAKE_TMUX = """#!/usr/bin/env bash
# Records every invocation; simulates has-session against a session list file.
echo "$*" >> "$FAKE_TMUX_LOG"
args="$*"
if [[ "$args" == *has-session* ]]; then
    # invoked as: tmux has-session -t =cc-N
    name=""
    prev=""
    for a in "$@"; do
        if [[ "$prev" == "-t" ]]; then name="$a"; fi
        prev="$a"
    done
    name="${name#=}"
    if [[ -n "${FAKE_SESSION_VANISHES:-}" ]]; then
        # Models a session that goes away mid-run: succeeds until the Nth call,
        # fails after. Without this the `-A` create-on-attach race is not
        # expressible and the guard against it cannot be tested.
        s=0; [[ -f "$FAKE_TMUX_SESSIONS_N" ]] && s=$(cat "$FAKE_TMUX_SESSIONS_N")
        echo $(( s + 1 )) > "$FAKE_TMUX_SESSIONS_N"
        if (( s + 1 >= FAKE_SESSION_VANISHES )); then exit 1; fi
    fi
    [[ -f "$FAKE_TMUX_SESSIONS" ]] && grep -qxF "$name" "$FAKE_TMUX_SESSIONS" && exit 0
    exit 1
fi
if [[ "$args" == *display-message* && "$args" == *session_attached* ]]; then
    # MODELS REAL TMUX, which is the point. MEASURED on 3.4: this context
    # renders session_attached EMPTY. The fake used to print a number here,
    # which made a door querying the WRONG way pass the suite while refusing
    # every heal on a real machine — a fake more permissive than reality is
    # worse than no fake.
    echo ""
    exit 0
fi
if [[ "$args" == *"list-sessions -F #{session_name} #{session_attached}"* ]]; then
    # The context where tmux DOES populate it: one row per session. Read the
    # session list from the same FILE has-session uses, not a pre-computed env
    # var — the fixture builds the environment before a test writes that file.
    if [[ -f "$FAKE_TMUX_SESSIONS" ]]; then
        while read -r _n; do
            [[ -n "$_n" ]] && echo "$_n 0"
        done < "$FAKE_TMUX_SESSIONS"
    fi
    exit 0
fi
if [[ "$args" == *list-panes* && -n "${FAKE_TMUX_LIST_PANES_FAIL:-}" ]]; then
    # A real tmux exits 1 for a session that has gone away. The fake used to
    # exit 0 unconditionally here, which made the door's failure path
    # structurally untestable — a green suite that proved nothing about it.
    exit 1
fi
if [[ "$args" == *list-panes* ]]; then
    # 'pane_id pane_pid pane_current_command' rows, one per pane. Absent file
    # -> no output, which exercises the probe's "cannot enumerate" path.
    # FAKE_TMUX_PANES2, when set, is served from the SECOND call on — this is
    # how "the pane changed between the decision and the keystrokes" is
    # reproduced.
    n=0; [[ -f "$FAKE_TMUX_PANES_N" ]] && n=$(cat "$FAKE_TMUX_PANES_N")
    echo $(( n + 1 )) > "$FAKE_TMUX_PANES_N"
    if [[ $n -ge 2 && -n "${FAKE_TMUX_PANES3:-}" && -f "$FAKE_TMUX_PANES3" ]]; then
        # Served from the THIRD call on: the pane list is read to decide, again
        # to build the prompt's disclosure, and again after the answer. Only
        # this can express "it changed AFTER the operator agreed".
        cat "$FAKE_TMUX_PANES3"
    elif [[ $n -ge 1 && -n "${FAKE_TMUX_PANES2:-}" && -f "$FAKE_TMUX_PANES2" ]]; then
        cat "$FAKE_TMUX_PANES2"
    elif [[ -f "$FAKE_TMUX_PANES" ]]; then
        cat "$FAKE_TMUX_PANES"
    fi
    exit 0
fi
if [[ "$args" == *respawn-pane* && -n "${FAKE_TMUX_RESPAWN_FAIL:-}" ]]; then
    # An undeliverable relaunch (pane gone, server dying). Logged above, so a
    # test can still see the attempt, and the door must report rather than
    # claim a heal.
    exit 1
fi
if [[ "$args" == *set-environment* && -n "${FAKE_TMUX_SETENV_FAIL:-}" ]]; then
    # Session env could not be set. Respawning anyway would relaunch with
    # STALE values — the exact defect the respawn design removes — so the door
    # must stand the heal down.
    exit 1
fi
if [[ "$args" == *new-session* && -n "${FAKE_TMUX_FDS:-}" ]]; then
    # Record which files the exec'd tmux client inherits open descriptors to;
    # a leaked heal-lock fd would hold the flock for the whole session.
    for f in /proc/$$/fd/*; do readlink "$f"; done >> "$FAKE_TMUX_FDS" 2>/dev/null
fi
if [[ "$args" == *list-sessions* ]]; then
    # The listing file stores 'name|attached|activity' lines; emit the shape
    # the requested -F format would produce.
    [[ -f "$FAKE_TMUX_LIST" ]] || exit 1
    if [[ "$args" == *session_attached* || "$args" == *session_activity* ]]; then
        cat "$FAKE_TMUX_LIST"
    else
        cut -d'|' -f1 "$FAKE_TMUX_LIST"
    fi
    exit 0
fi
exit 0
"""


@pytest.fixture()
def door(tmp_path):
    """Run cc-slot.sh with a fake tmux + isolated HOME; return (run, log)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(_FAKE_TMUX)
    fake_tmux.chmod(fake_tmux.stat().st_mode | stat.S_IEXEC)

    home = tmp_path / "home"
    home.mkdir()
    # cc-slot resolves GENESIS_ROOT from HOME, and consults a venv python for
    # the liveness verdict. Fake it so the door's branch can be driven directly;
    # the probe's own logic is unit-tested in tests/test_cc/test_slot_liveness.py.
    venv_bin = home / "genesis" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        # Ordered call log: which probe ran, in which order. The ORDER is the
        # invariant under test — the verdict that gates a destructive action
        # must be the freshest one taken.
        'if [[ "$*" == *slot_liveness* ]]; then\n'
        '  echo liveness >> "$FAKE_PROBE_LOG"\n'
        # Lets a probe BURN TIME. Without it the slot map's whole-map deadline
        # cannot be exercised at all: every fake probe returns instantly, so a
        # per-probe budget and a shared one are indistinguishable.
        '  if [[ -n "${FAKE_PROBE_SLEEP:-}" ]]; then sleep "$FAKE_PROBE_SLEEP"; fi\n'
        "fi\n"
        'if [[ "$*" == *slot_liveness* ]]; then\n'
        # FAKE_LIVENESS may be a comma-separated SEQUENCE; each call consumes
        # the next entry (last one repeats). This is how the probe/re-probe
        # race is reproduced.
        '  n=0; [[ -f "$FAKE_LIVENESS_N" ]] && n=$(cat "$FAKE_LIVENESS_N")\n'
        '  IFS="," read -ra _v <<< "${FAKE_LIVENESS:-}"\n'
        '  idx=$n; (( idx >= ${#_v[@]} )) && idx=$(( ${#_v[@]} - 1 ))\n'
        '  echo "${_v[$idx]:-}"\n'
        '  echo "note"\n'
        '  echo $(( n + 1 )) > "$FAKE_LIVENESS_N"\n'
        "  exit 0\n"
        "fi\n"
        # The capacity gate, when a test wants it to answer. A rebuild STARTS a
        # claude, so it consults this the way a create does.
        'if [[ "$*" == *session_cap* && -n "${FAKE_HEAL_CAP:-}" ]]; then\n'
        '  echo "session_cap $*" >> "$FAKE_PROBE_LOG"\n'
        '  c=0; [[ -f "$FAKE_HEAL_CAP_N" ]] && c=$(cat "$FAKE_HEAL_CAP_N")\n'
        '  IFS=";" read -ra _c <<< "$FAKE_HEAL_CAP"\n'
        '  ci=$c; (( ci >= ${#_c[@]} )) && ci=$(( ${#_c[@]} - 1 ))\n'
        '  echo $(( c + 1 )) > "$FAKE_HEAL_CAP_N"\n'
        '  _one="${_c[$ci]}"\n'
        '  printf \'%s\\n\' "${_one%%|*}" "cap message" "${_one#*|}"; exit 0\n'
        "fi\n"
        # Any other module (the capacity gate with no fake, the login gate) ->
        # no verdict, which those paths already treat as unavailable.
        "exit 1\n"
    )
    fake_py.chmod(fake_py.stat().st_mode | stat.S_IEXEC)
    fake_flock = bin_dir / "flock"
    # A lock guarding a DESTRUCTIVE action cannot be best-effort, so its failure
    # must be reachable in a test — without this the whole `_acquire_heal_lock`
    # failure path is unexercised, which is how it shipped claiming to be closed.
    fake_flock.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -n "${FAKE_FLOCK_FAIL:-}" ]]; then exit 1; fi\n'
        'exec /usr/bin/flock "$@"\n'
    )
    fake_flock.chmod(0o755)
    log = tmp_path / "tmux.log"
    sessions = tmp_path / "sessions.txt"
    listing = tmp_path / "list.txt"
    panes = tmp_path / "panes.txt"

    def _env() -> dict:
        return {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_TMUX_LOG": str(log),
            "FAKE_TMUX_SESSIONS": str(sessions),
            "FAKE_TMUX_SESSIONS_N": str(tmp_path / "sess_calls.txt"),
            "FAKE_TMUX_LIST": str(listing),
            "FAKE_TMUX_PANES": str(panes),
            "FAKE_LIVENESS": os.environ.get("_TEST_FAKE_LIVENESS", ""),
            "FAKE_LIVENESS_N": str(tmp_path / "liveness_calls.txt"),
            "FAKE_PROBE_LOG": str(tmp_path / "probe_order.txt"),
            "FAKE_PROBE_SLEEP": os.environ.get("_TEST_FAKE_PROBE_SLEEP", ""),
            "FAKE_TMUX_PANES_N": str(tmp_path / "panes_calls.txt"),
            "FAKE_TMUX_PANES2": os.environ.get("_TEST_FAKE_PANES2", ""),
            "FAKE_TMUX_PANES3": os.environ.get("_TEST_FAKE_PANES3", ""),
            "FAKE_TMUX_LIST_PANES_FAIL": os.environ.get("_TEST_FAKE_LIST_PANES_FAIL", ""),
            "FAKE_TMUX_RESPAWN_FAIL": os.environ.get("_TEST_FAKE_RESPAWN_FAIL", ""),
            "FAKE_TMUX_SETENV_FAIL": os.environ.get("_TEST_FAKE_SETENV_FAIL", ""),
            "FAKE_TMUX_FDS": str(tmp_path / "tmux_fds.txt"),
            "FAKE_FLOCK_FAIL": os.environ.get("_TEST_FAKE_FLOCK_FAIL", ""),
            "FAKE_HEAL_CAP": os.environ.get("_TEST_FAKE_HEAL_CAP", ""),
            "FAKE_HEAL_CAP_N": str(tmp_path / "cap_calls.txt"),
            "FAKE_SESSION_VANISHES": os.environ.get("_TEST_FAKE_VANISH", ""),
        }

    def _reset_counters() -> None:
        # Per-invocation call counters: two runs in one test must not leak
        # sequence positions (FAKE_LIVENESS / FAKE_TMUX_PANES2) into each
        # other. BOTH runners must do this — the pty runner skipping it made
        # probe-order assertions depend on whether a piped run preceded them.
        (tmp_path / "liveness_calls.txt").unlink(missing_ok=True)
        (tmp_path / "probe_order.txt").unlink(missing_ok=True)
        (tmp_path / "panes_calls.txt").unlink(missing_ok=True)
        (tmp_path / "cap_calls.txt").unlink(missing_ok=True)
        (tmp_path / "sess_calls.txt").unlink(missing_ok=True)

    def run(*args: str) -> subprocess.CompletedProcess:
        env = _env()
        _reset_counters()
        # Deliberately NOT inheriting os.environ: the test itself may run
        # inside a cc slot, whose GENESIS_CC_PERMISSION_MODE / TMUX would
        # contaminate the branch under test.
        return subprocess.run(
            ["bash", str(_CC_SLOT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            # DELIBERATE: without this, stdin is inherited from pytest, so the
            # rebuild confirm's `-t 0` test would answer "there is a human"
            # when the suite is run from a terminal and "there is not" in CI.
            # The branch under test would then depend on how the suite was
            # launched. DEVNULL pins it to the no-terminal path; the pty runner
            # below is how the WITH-a-human path is exercised.
            stdin=subprocess.DEVNULL,
            # start_new_session detaches the CONTROLLING TERMINAL, which is what
            # the confirm actually tests (`: >/dev/tty`). Without it the branch
            # depends on whether pytest itself was launched from a terminal —
            # green in CI, and on a dev box the door would sit at a prompt.
            start_new_session=True,
        )

    def run_tty(answer: str, *args: str, stdin_text: str | None = None,
                wait_for: str | None = None):
        """Run the door under a REAL pty and answer the rebuild confirm.

        The confirm needs a controlling terminal and reads from /dev/tty, so a
        piped runner measures the no-terminal branch instead of the code under
        test. A pty merges the two streams, so `stdout` and `stderr` carry the
        same combined text here.

        `stdin_text` gives the child a stdin that is NOT the terminal — used to
        prove the answer is taken from /dev/tty and never from stdin.

        `wait_for` holds the answer back until that text has appeared. Writing
        immediately is fine for ordinary answers (the tty buffers them), but it
        is WRONG for a control character: the line discipline raises SIGINT
        when the byte is RECEIVED, so a \x03 written before the prompt exists
        is just buffered and later read as data. A Ctrl-C test that sends early
        passes whether or not the door traps SIGINT — MEASURED: it did.
        """
        env = _env()
        _reset_counters()
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child exec's away
            if stdin_text is not None:
                r_fd, w_fd = os.pipe()
                os.write(w_fd, stdin_text.encode())
                os.close(w_fd)
                os.dup2(r_fd, 0)
            os.execve("/bin/bash", ["bash", str(_CC_SLOT), *args], env)  # noqa: S606
        chunks: list[bytes] = []
        sent = False
        try:
            if wait_for is None:
                os.write(fd, (answer + "\n").encode())
                sent = True
            # Read to EOF (every slave fd closed => the child is gone), with a
            # deadline enforced by select. Checking waitpid BEFORE a blocking
            # read raced: the child could exit with output still buffered, so
            # assertions on that output were a latent flake, and the deadline
            # was never evaluated while the read blocked.
            deadline = time.monotonic() + 30
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # pragma: no cover - only on a hang
                    os.kill(pid, 9)
                    break
                ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if not sent and wait_for is not None \
                        and wait_for in b"".join(chunks).decode(errors="replace"):
                    os.write(fd, answer.encode() if answer == "\x03"
                             else (answer + "\n").encode())
                    sent = True
        finally:
            os.close(fd)
            _done, status = os.waitpid(pid, 0)
            rc = os.waitstatus_to_exitcode(status)
        text = b"".join(chunks).decode(errors="replace")
        return subprocess.CompletedProcess(args, rc, stdout=text, stderr=text)

    run.tty = run_tty
    run.probe_log = tmp_path / "probe_order.txt"
    run.fds_file = tmp_path / "tmux_fds.txt"
    run.panes2 = tmp_path / "panes2.txt"
    return run, log, sessions, listing, panes


def _new_session_line(log: Path) -> str:
    lines = [ln for ln in log.read_text().splitlines() if "new-session" in ln]
    assert len(lines) == 1, f"expected exactly one new-session call, got: {lines}"
    return lines[0]


class TestManualMode:
    def test_first_free_slot_is_cc_1(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "-A" in line
        assert "-s cc-1" in line
        assert "GENESIS_SLOT=1" in line
        assert "--permission-mode auto" in line

    def test_allocation_skips_existing_sessions(self, door):
        run, log, sessions, listing, _panes = door
        sessions.write_text("cc-1\ncc-2\n")
        listing.write_text("cc-1|1|Thu Jul 16 20:00:00 2026\ncc-2|0|Thu Jul 16 19:00:00 2026\n")
        result = run("manual")
        assert result.returncode == 0, result.stderr
        assert "-s cc-3" in _new_session_line(log)
        # The slot map names existing sessions with attach state.
        assert "cc-1  attached" in result.stderr
        assert "cc-2  detached" in result.stderr
        assert "tmux attach" in result.stderr

    def test_slot_map_marks_a_session_with_no_claude(self, door):
        """The printed map is the door's only honest surface for a slot the
        door itself will not heal.

        Manual allocation deliberately never grabs an existing session (it
        might be someone's on purpose), so only the hostname door heals its own
        slot. That makes the map the place to SAY a slot has no claude — and to
        point at the door rather than at `tmux attach`, which reattaches to the
        bare prompt and heals nothing.
        """
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("%0 4242 bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "no claude" in proc.stderr, (
            f"a claude-less slot was listed as if it were healthy:\n{proc.stderr}"
        )

    def test_slot_map_leaves_a_live_slot_unannotated(self, door):
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|1|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("%0 4242 bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "ALIVE"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert "cc-1  attached" in proc.stderr, proc.stderr
        assert "no claude" not in proc.stderr, (
            f"a live slot was wrongly annotated:\n{proc.stderr}"
        )

    def test_slot_map_is_silent_when_the_probe_cannot_run(self, door):
        """No verdict must never render as a verdict — and the map must not
        pay for a probe it cannot make."""
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("%0 4242 bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = ""
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "cc-1" in proc.stderr
        assert "no claude" not in proc.stderr, (
            f"an absent verdict was rendered as one:\n{proc.stderr}"
        )

    def test_the_door_survives_a_session_dying_during_the_slot_map(self, door):
        """A session can vanish between listing it and inspecting it — which is
        exactly what happens when the last slot's claude exits and the tmux
        server shuts down.

        The script runs under `set -euo pipefail`, so `var=$(tmux ... | tr ...)`
        aborts the WHOLE door when tmux exits non-zero: `pipefail` promotes the
        failure past the succeeding filter and `set -e` kills the script with no
        message. On the manual door — the dashboard web terminal and manual SSH
        — that drops the operator at a bare prompt with no claude and no error.
        """
        run, log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("%0 4242 bash\n")
        os.environ["_TEST_FAKE_LIST_PANES_FAIL"] = "1"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIST_PANES_FAIL", None)
        assert proc.returncode == 0, (
            f"the door died when a listed session went away:\n{proc.stderr}"
        )
        assert "new-session" in log.read_text(), (
            f"the door never reached the launch:\n{proc.stderr}"
        )

    def test_has_session_probes_use_exact_name_match(self, door):
        run, log, sessions, _listing, _panes = door
        sessions.write_text("cc-1\n")
        run("manual")
        probes = [ln for ln in log.read_text().splitlines() if "has-session" in ln]
        assert probes, "allocation must probe has-session"
        # '=' prefix: without it tmux prefix-matches, so cc-1 reads as
        # existing whenever only cc-10 does.
        assert all("-t =cc-" in p for p in probes), probes

    def test_extra_args_are_forwarded_into_the_slot(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "--resume", "abc123")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--resume abc123" in line
        assert "--permission-mode auto" in line  # no perm flag supplied -> default kept

    def test_caller_permission_flag_suppresses_default(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "--dangerously-skip-permissions")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--dangerously-skip-permissions" in line
        assert "--permission-mode auto" not in line

    def test_permission_mode_equals_form_also_suppresses(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "--permission-mode=plan")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--permission-mode=plan" in line
        assert "--permission-mode auto" not in line

    def test_retired_manual_sessions_do_not_consume_cap(self, door):
        # Legacy cc-manual-<ts>-<pid> sessions from the old wrapper must not
        # count toward the slot cap: manual allocation can only ever create
        # numeric cc-N, so counting strays would falsely lock installs out.
        run, log, sessions, listing, _panes = door
        sessions.write_text("cc-1\n")
        listing.write_text(
            "cc-1|1|Thu Jul 16 20:00:00 2026\n"
            "cc-manual-1784246386-1229981|0|Thu Jul 16 19:59:46 2026\n"
        )
        result = run("manual")
        assert result.returncode == 0, result.stderr
        assert "-s cc-2" in _new_session_line(log)
        # Only the numeric slot counts: the header reads `live: 1`, not `live: 2`
        # (the retired cc-manual-* stray is excluded from the count).
        assert "live: 1" in result.stderr

    def test_exotic_arg_survives_quoting(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "--resume", "a b'c")
        assert result.returncode == 0, result.stderr
        # %q-quoted through the tmux command string: the shell inside the
        # session must receive the original value as ONE argument. Parse only the
        # claude-args segment — between 'claude ' and the '; __ec=' exit-capture
        # trailer the inner command now appends.
        line = _new_session_line(log)
        cmd = line.split("LANG=", 1)[1].split(" ", 1)[1]
        claude_args = cmd.split("claude", 1)[1].split("; __ec=", 1)[0]
        parsed = subprocess.run(
            ["bash", "-c", f'set -- {claude_args}; printf "%s\\n" "$@"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "a b'c" in parsed.stdout.splitlines()

    def test_inner_command_wires_exit_capture(self, door):
        """The inner tmux command drops `exec` and records claude's exit via
        cc_exit_capture.sh before the pane vanishes, preserving claude's code as
        the pane's exit. Locks the wiring the 2026-08-19 death observability adds."""
        run, log, _sessions, _listing, _panes = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "cc_exit_capture.sh 1 $__ec" in line, line  # slot 1, deferred code
        assert "exit $__ec" in line, line  # claude's code reproduced as the pane's
        assert "exec claude" not in line, "inner exec must be dropped so the trailer runs"


class TestSlotMapBudget:
    """The slot map is COSMETIC and must never be what makes a login feel slow."""

    def test_one_deadline_is_shared_across_probes(self, door):
        """A per-probe timeout is not a budget.

        The give-up flag only trips when a probe actually hits its own ceiling,
        so N slots that each finish JUST under it still cost N x budget —
        measured at roughly 20s across seven slots at ~2.9s each, with nothing
        ever timing out. Twelve slots at 1s each here: with a shared deadline
        the map stops probing partway through, so strictly fewer than twelve
        probes run.
        """
        run, _log, _sessions, listing, panes = door
        listing.write_text("".join(f"cc-{i}|0|ts\n" for i in range(1, 13)))
        # The map reads pane pids before probing; with no panes there is
        # nothing to probe and the budget is never exercised.
        panes.write_text("%0 4242 bash\n")
        os.environ["_TEST_FAKE_PROBE_SLEEP"] = "1"
        try:
            result = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_PROBE_SLEEP", None)
        # The cap denies a 12th session and exits non-zero; irrelevant here —
        # the map is printed BEFORE that decision and is what is under test.
        assert "Existing slots" in result.stderr
        probes = run.probe_log.read_text().split().count("liveness")
        assert probes < 12, (
            f"every slot was probed ({probes}); the map is spending per-probe "
            f"budget rather than one shared deadline"
        )


class TestHostnameMode:
    def test_hostname_parses_trailing_slot(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("genesis-3-4")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "-s cc-4" in line
        assert "GENESIS_SLOT=4" in line

    def test_invalid_hostname_rejected(self, door):
        run, _log, _sessions, _listing, _panes = door
        result = run("genesis-abc")
        assert result.returncode == 1
        assert "Invalid slot" in result.stderr


class TestScriptHygiene:
    def test_the_rebuild_prompt_is_bounded(self):
        """A SOURCE contract, and deliberately so.

        `read -t 120` bounds an abandoned prompt. Its behaviour cannot honestly
        be tested — a real assertion would have to wait two minutes — and
        MEASURED, dropping the flag left the entire suite green. Pinning the
        text is weaker than pinning behaviour and is stated as such, but it is
        strictly better than the nothing that was here before.
        """
        text = Path(_CC_SLOT).read_text()
        read_lines = [ln for ln in text.splitlines()
                      if "Rebuild this slot?" in ln and "read " in ln]
        assert len(read_lines) == 1, f"expected one confirm read, got {read_lines}"
        assert "-t 120" in read_lines[0], (
            f"the rebuild prompt is unbounded — an abandoned prompt would hold "
            f"the door open forever:\n{read_lines[0]}"
        )

    def test_cc_slot_syntax_clean(self):
        subprocess.run(["bash", "-n", str(_CC_SLOT)], check=True, timeout=10)

    def test_bootstrap_syntax_clean(self):
        subprocess.run(["bash", "-n", str(_BOOTSTRAP)], check=True, timeout=10)


class TestBootstrapWrapper:
    """The bashrc block bootstrap installs between the tmux-wrap sentinels."""

    @pytest.fixture()
    def block(self) -> str:
        text = _BOOTSTRAP.read_text()
        begin = text.index("# >>> genesis tmux-wrap >>>")
        end = text.index("# <<< genesis tmux-wrap <<<")
        return text[begin:end]

    def test_wrapper_delegates_to_cc_slot_manual(self, block):
        assert 'cc-slot.sh" manual "$@"' in block
        # The old design this replaces must not creep back.
        assert "cc-manual-" not in block

    def test_wrapper_keeps_passthrough_and_optout(self, block):
        assert "-p|--print|--version|-v|--help|-h" in block
        assert "GENESIS_NO_TMUX_WRAP" in block
        # Never wrap inside tmux (slot panes run claude directly).
        assert '-z "${TMUX:-}"' in block

    def test_sentinels_unchanged_for_auto_migration(self):
        # The refresh path keys on these exact markers; renaming them would
        # strand the old orphan-minting wrapper on every existing install.
        text = _BOOTSTRAP.read_text()
        assert text.count("# >>> genesis tmux-wrap >>>") >= 2  # heredoc + grep
        assert "tmux-wrap block refreshed" in text

    def test_retired_registry_cleanup_present(self):
        text = _BOOTSTRAP.read_text()
        assert 'rm -rf "$HOME/.genesis/session-owners"' in text
        assert 'rm -f "$HOME/.genesis/session-guard.disabled"' in text


class TestSlotHeal:
    """`tmux new-session -A` attaches to an existing session and DISCARDS the
    command, so a slot sitting at a bare shell can never relaunch itself. The
    door probes for a live claude and types the canonical launch line into the
    pane when there is none.

    The verdict itself is faked here (the probe's logic is unit-tested in
    tests/test_cc/test_slot_liveness.py); these lock what the DOOR does with it.
    """

    def _run_with(
        self,
        door,
        verdict,
        panes_text="%0 4242 bash\n",
        session="cc-4",
        panes2_text=None,
        heal_cap="ALLOW|ok",
        vanish=None,
        flock_fail=False,
        stdin_text=None,
        wait_for=None,
        panes3_text=None,
        respawn_fail=False,
        setenv_fail=False,
        confirm="y",
    ):
        """`confirm` is the operator's answer at the rebuild prompt.

        Default "y" so the heal tests below exercise the relaunch. Pass None to
        run WITHOUT a terminal at all — the branch a dashboard or background
        entrance takes, which must never destroy a pane.
        """
        run, log, sessions, _listing, panes = door
        sessions.write_text(f"{session}\n")
        panes.write_text(panes_text)
        os.environ["_TEST_FAKE_LIVENESS"] = verdict
        if panes2_text is not None:
            run.panes2.write_text(panes2_text)
            os.environ["_TEST_FAKE_PANES2"] = str(run.panes2)
        if heal_cap:
            os.environ["_TEST_FAKE_HEAL_CAP"] = heal_cap
        if vanish is not None:
            os.environ["_TEST_FAKE_VANISH"] = str(vanish)
        if flock_fail:
            os.environ["_TEST_FAKE_FLOCK_FAIL"] = "1"
        if panes3_text is not None:
            p3 = run.panes2.parent / "panes3.txt"
            p3.write_text(panes3_text)
            os.environ["_TEST_FAKE_PANES3"] = str(p3)
        if respawn_fail:
            os.environ["_TEST_FAKE_RESPAWN_FAIL"] = "1"
        if setenv_fail:
            os.environ["_TEST_FAKE_SETENV_FAIL"] = "1"
        door_arg = f"genesis-3-{session.split('-')[1]}"
        try:
            proc = (
                run(door_arg) if confirm is None
                else run.tty(confirm, door_arg, stdin_text=stdin_text, wait_for=wait_for)
            )
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_PANES2", None)
            os.environ.pop("_TEST_FAKE_HEAL_CAP", None)
            os.environ.pop("_TEST_FAKE_VANISH", None)
            os.environ.pop("_TEST_FAKE_FLOCK_FAIL", None)
            os.environ.pop("_TEST_FAKE_PANES3", None)
            os.environ.pop("_TEST_FAKE_RESPAWN_FAIL", None)
            os.environ.pop("_TEST_FAKE_SETENV_FAIL", None)
        return proc, log.read_text()

    def test_poisoned_slot_is_healed_with_the_full_launch_line(self, door):
        _proc, log = self._run_with(door, "POISONED")
        respawn = [ln for ln in log.splitlines() if "respawn-pane" in ln]
        assert respawn, f"no heal attempted:\n{log}"
        payload = respawn[0]
        # The healed pane must run the SAME thing a fresh slot runs: cd guard,
        # permission flag, exit-capture trailer, exit-code preservation.
        for fragment in ("cd ", "claude ", "--permission-mode auto",
                         "cc_exit_capture.sh 4", "; __ec=", "exit $__ec"):
            assert fragment in payload, f"missing {fragment!r} in heal payload:\n{payload}"

    def test_the_heal_is_one_atomic_command_and_types_nothing(self, door):
        """Replaces a test of the C-c / C-u / type / Enter ladder.

        That ladder existed to re-create by hand what `new-session` provides,
        and review found four separate members of that obligation across three
        rounds. `respawn-pane` runs the command from the tmux SERVER's
        environment exactly as create does, so there is one command and its
        exit status IS the delivery check.

        The send-keys assertion is kept as a TOMBSTONE: reintroducing the
        keystroke path would silently restore the whole class this removed.
        """
        _proc, log = self._run_with(door, "POISONED")
        respawn = [ln for ln in log.splitlines() if "respawn-pane" in ln]
        assert len(respawn) == 1, f"expected exactly one respawn:\n{log}"
        assert "-k" in respawn[0], "respawn must replace the pane's process"
        assert "send-keys" not in log, f"the keystroke ladder came back:\n{log}"

    def test_a_failed_environment_set_stands_the_heal_down(self, door):
        """Respawning with STALE env is the exact defect this design removes,
        so a failed set must not fall through to the relaunch."""
        proc, log = self._run_with(door, "POISONED", setenv_fail=True)
        assert "respawn-pane" not in log, f"relaunched with stale env:\n{log}"
        assert "stale values" in proc.stderr, proc.stderr

    def test_heal_prints_an_honest_message(self, door):
        proc, _log = self._run_with(door, "POISONED")
        assert "running no claude" in proc.stderr, proc.stderr

    def test_alive_slot_is_attached_untouched(self, door):
        proc, log = self._run_with(door, "ALIVE")
        assert "respawn-pane" not in log, f"typed into a LIVE session:\n{log}"
        assert "running no claude" not in proc.stderr

    def test_an_alive_verdict_stops_before_the_lock(self, door):
        """Locks the FIRST liveness gate specifically, by COUNTING probes.

        MEASURED by mutation: deleting the `= POISONED` condition from the
        decision gate left `test_alive_slot_is_attached_untouched` green,
        because the re-probe taken under the lock also demands POISONED and
        caught it there. That test therefore could not fail on the mechanism it
        names — the same defect as asserting only the last element of a probe
        ORDER. Behaviour was never wrong; the coverage was.

        An ALIVE first verdict must short-circuit: exactly ONE probe, no lock,
        no second probe. Two probes means the door entered the heal path for a
        slot that already had a claude in it.
        """
        run, _log, _sessions, _listing, _panes = door
        _proc, log = self._run_with(door, "ALIVE")
        assert "respawn-pane" not in log
        order = run.probe_log.read_text().split()
        assert order == ["liveness"], (
            f"an ALIVE slot entered the heal path instead of short-circuiting: {order}"
        )

    def test_unknown_verdict_spares_the_session(self, door):
        _proc, log = self._run_with(door, "UNKNOWN")
        assert "respawn-pane" not in log, f"typed into a session of unknown state:\n{log}"

    def test_empty_verdict_spares_the_session(self, door):
        # Broken venv / timeout / crashed probe -> no verdict at all.
        _proc, log = self._run_with(door, "")
        assert "respawn-pane" not in log, f"typed on an absent verdict:\n{log}"

    def test_garbage_verdict_spares_the_session(self, door):
        _proc, log = self._run_with(door, "banana")
        assert "respawn-pane" not in log

    def test_unenumerable_panes_spares_the_session(self, door):
        _proc, log = self._run_with(door, "POISONED", panes_text="")
        assert "respawn-pane" not in log, f"healed without knowing which pane:\n{log}"

    def test_heal_targets_the_first_windows_pane(self, door):
        # list-panes -s lists in window order, so row 1 is window 1's pane —
        # where the canonical launch lives.
        _proc, log = self._run_with(door, "POISONED", panes_text="%5 111 bash\n%7 222 bash\n")
        sk = [ln for ln in log.splitlines() if "respawn-pane" in ln]
        assert all("%5" in ln for ln in sk), f"heal aimed at the wrong pane:\n{sk}"

    def test_heal_consults_the_oauth_gate(self, door):
        """A heal starts a FRESH claude, so it needs the same token treatment a
        create does — the pre-existing `_SESSION_EXISTS` shortcut would have
        skipped it, on the reasoning that an attach never re-runs the pane
        command. That reasoning stops holding the moment the door can relaunch.
        """
        _proc, log = self._run_with(door, "POISONED")
        gate = (Path(_CC_SLOT).read_text())
        assert '[ "$_HEAL" = "1" ]' in gate.split("_OAUTH_SRC=\"\"")[1].split("fi")[0], (
            "the OAuth gate condition does not include the heal path"
        )
        assert "respawn-pane" in log

    def test_alive_attach_still_skips_the_oauth_gate(self, door):
        # An attach does not re-run the pane command, so injecting a token
        # there would be inert and would waste a probe.
        _proc, log = self._run_with(door, "ALIVE")
        assert "respawn-pane" not in log

    def test_heal_does_not_silence_the_doors_own_stderr(self, door):
        """Regression: the per-slot lock is opened with `exec`, whose
        redirections apply to the SHELL. A `2>/dev/null` on that line silenced
        every later notice for the rest of the run — the heal still happened,
        so send-keys assertions stayed green while the operator was told
        nothing. Assert a message that is emitted AFTER the lock is taken.
        """
        proc, log = self._run_with(door, "POISONED")
        assert "respawn-pane" in log
        assert proc.stderr.strip(), "the door went silent after taking the lock"
        assert "relaunching" in proc.stderr, proc.stderr

    def test_still_exactly_one_new_session_on_the_heal_path(self, door):
        _proc, log = self._run_with(door, "POISONED")
        assert len([ln for ln in log.splitlines() if "new-session" in ln]) == 1

    def test_a_stood_down_heal_still_probed_in_the_right_order(self, door):
        """Ordering must not depend on the happy path."""
        run, _log, _sessions, _listing, _panes = door
        _proc, log = self._run_with(door, "POISONED", confirm="n")
        assert "respawn-pane" not in log
        order = [x for x in run.probe_log.read_text().split() if x == "liveness"]
        # TWO liveness probes, and the second one matters: it is taken under
        # the lock, after the OAuth gate, so a slot that came alive while the
        # door was preparing is never respawned. There is no third probe any
        # more — the question the deleted `--idle` probe asked is now put to
        # the operator, and a declined prompt must not skip the re-check.
        assert order == ["liveness", "liveness"], (
            f"the declined path did not re-probe liveness under the lock: {order}"
        )

    def test_malformed_pane_id_is_never_a_send_keys_target(self, door):
        _proc, log = self._run_with(door, "POISONED", panes_text="garbage 4242 bash\n")
        assert "respawn-pane" not in log, f"typed at a non-pane target:\n{log}"

    def test_slot_that_came_alive_during_preparation_is_not_typed_into(self, door):
        """The verdict is re-confirmed immediately before the keystrokes.

        Up to ~30s elapses between the decision and send-keys (the OAuth gate
        sits in between), which is ample for a second door — or the operator in
        another terminal — to have started claude in this very pane. Typing a
        command line into a live Claude Code TUI is the one failure this change
        must never cause, so a stale POISONED must not be acted on.
        """
        _proc, log = self._run_with(door, "POISONED,ALIVE")
        assert "respawn-pane" not in log, f"typed into a slot that came alive:\n{log}"

    def test_race_stand_down_is_announced(self, door):
        proc, _log = self._run_with(door, "POISONED,ALIVE")
        assert "came alive while preparing" in proc.stderr, proc.stderr

    def test_still_heals_when_the_recheck_agrees(self, door):
        _proc, log = self._run_with(door, "POISONED,POISONED")
        assert "respawn-pane" in log, f"re-check wrongly blocked a real heal:\n{log}"

    # ── G1: idleness is CURRENT state, established immediately before keys ──

    def test_the_prompt_discloses_the_FRESH_pane_command(self, door):
        """The operator can only decide from what they are TOLD.

        The pane list is captured when the heal is DECIDED, but the OAuth gate
        is allowed up to 30s and the lock up to 45s before the prompt appears —
        ample for the operator to have started vim in that pane. Disclosing the
        stale snapshot would put "bash" in front of a person whose pane is now
        running an editor, and their "y" would be uninformed. This is the same
        freshness property the deleted safety gate carried; it moved from a
        machine verdict to the sentence the human reads.
        """
        proc, _log = self._run_with(
            door, "POISONED",
            panes_text="%0 4242 bash\n",
            panes2_text="%0 4242 vim\n",
            confirm="n",
        )
        assert "'vim'" in proc.stdout, (
            f"prompt disclosed the stale pane snapshot:\n{proc.stdout}"
        )

    def test_pane_replaced_mid_flight_is_not_typed_into(self, door):
        # The first window's pane id changed between decision and action —
        # the layout moved under us; whatever is there now was never probed.
        _proc, log = self._run_with(
            door, "POISONED",
            panes_text="%0 4242 bash\n",
            panes2_text="%9 5555 bash\n",
        )
        assert "respawn-pane" not in log, f"typed into a pane never probed:\n{log}"

    # ── G2: the heal path must be EQUIVALENT to the create path ─────────────

    def test_healed_pane_receives_the_create_paths_env(self, door):
        """`tmux new-session` passes GENESIS_SLOT / GENESIS_CC_PERMISSION_MODE /
        CLAUDE_CODE_TMPDIR / LANG via `-e`; a healed pane keeps whatever stale
        environment the old shell had unless the heal carries the same set.
        """
        _proc, log = self._run_with(door, "POISONED")
        setenv = [ln for ln in log.splitlines() if "set-environment" in ln]
        assert setenv, f"no session environment set:\n{log}"
        joined = "\n".join(setenv)
        # PATH is in this set BECAUSE of the switch to respawn: the command now
        # runs from the tmux SERVER's environment, whose PATH is right only by
        # luck of who started the server.
        for var in ("GENESIS_SLOT", "GENESIS_CC_PERMISSION_MODE",
                    "CLAUDE_CODE_TMPDIR", "TMPDIR", "PATH", "LANG"):
            assert var in joined, f"healed pane not given {var!r}:\n{joined}"
        # Set BEFORE the respawn — the respawned command reads it at exec.
        first_respawn = next(
            i for i, ln in enumerate(log.splitlines()) if "respawn-pane" in ln
        )
        last_setenv = max(
            i for i, ln in enumerate(log.splitlines()) if "set-environment" in ln
        )
        assert last_setenv < first_respawn, f"env set after the relaunch:\n{log}"

    def test_create_path_still_passes_env_via_dash_e(self, door):
        # Guards the refactor that unifies the two paths' env source.
        run, log, _sessions, _listing, _panes = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        for var in ("GENESIS_SLOT=1", "GENESIS_CC_PERMISSION_MODE=",
                    "CLAUDE_CODE_TMPDIR=", "TMPDIR=", "LANG="):
            assert f"-e {var}" in line, line

    def test_heal_lock_fd_is_not_inherited_by_the_tmux_client(self, door):
        """`exec tmux` replaces the shell, and an un-CLOEXEC'd lock fd rides
        along into the attached client — holding the per-slot flock for the
        whole session, so every later door to this slot stalls the full
        `flock -w 45` for nothing.
        """
        run, _log, _sessions, _listing, _panes = door
        _proc, log = self._run_with(door, "POISONED")
        assert "respawn-pane" in log  # the heal (and so the lock) happened
        fds = run.fds_file.read_text() if run.fds_file.exists() else ""
        assert fds.strip(), "fd recording never ran — shim regression"
        assert "cc-slot-heal" not in fds, (
            f"heal lock fd leaked into the tmux client:\n{fds}"
        )

    def test_an_undeliverable_relaunch_is_reported_not_claimed(self, door):
        """The respawn's exit status IS the delivery check.

        This is the shape's real advantage over the keystroke ladder it
        replaced: tmux could accept a keystroke that the shell then ignored, so
        delivery had to be inferred. A failed respawn is simply a non-zero
        exit, and the door must say so rather than attach while the operator
        expects claude.
        """
        proc, log = self._run_with(door, "POISONED", respawn_fail=True)
        assert "respawn-pane" in log, "no relaunch was attempted at all"
        assert "could not relaunch" in proc.stderr, proc.stderr


    # ---- the operator confirm -------------------------------------------
    # These replace the machine gates (idleness, attachment, shell-name
    # whitelist, capacity) deleted in this commit. Four review rounds produced
    # eight findings against those gates because the question they asked — "is
    # anyone using this pane" — is undecidable from outside it. The confirm
    # asks the one party who can answer.

    def test_without_a_terminal_the_door_never_destroys_a_pane(self, door):
        """The branch a dashboard or background entrance takes.

        There is no human to answer, so the destructive action is simply not
        available. A background session was never able to heal a slot; this
        keeps it exactly as capable as before while making it unable to do
        damage, rather than letting it destroy a pane nobody is watching.
        """
        proc, log = self._run_with(door, "POISONED", confirm=None)
        assert "respawn-pane" not in log, f"destroyed a pane with no human:\n{log}"
        assert "no terminal here" in proc.stderr
        assert "tmux kill-session" in proc.stderr, "did not name the manual repair"
        # Pins the GUARD, not just the outcome. Without `: >/dev/tty` the door
        # would print the prompt and then decline on a failed read — same final
        # state, so asserting only "no respawn" left the guard
        # mutation-transparent (MEASURED: the mutation survived 58/58).
        assert "Rebuild this slot?" not in proc.stderr, (
            f"prompted with no controlling terminal:\n{proc.stderr}"
        )
        # The probe must be SILENT when it fails. A bare `: >/dev/tty` leaks a
        # shell-level redirection error above the door's own explanation.
        assert "No such device" not in proc.stderr, (
            f"leaked a shell redirection error:\n{proc.stderr}"
        )

    def test_declining_the_rebuild_leaves_the_pane_alone(self, door):
        proc, log = self._run_with(door, "POISONED", confirm="n")
        assert "respawn-pane" not in log, f"rebuilt after a refusal:\n{log}"
        assert "left as-is" in proc.stdout

    def test_bare_enter_declines(self, door):
        """Default-deny, like `_cap_reclaim`'s ATTACHED confirm.

        Someone reconnecting on autopilot presses Enter. That must not be the
        answer that kills their process group.
        """
        _proc, log = self._run_with(door, "POISONED", confirm="")
        assert "respawn-pane" not in log, f"a bare Enter rebuilt the slot:\n{log}"

    def test_an_unrelated_answer_declines(self, door):
        _proc, log = self._run_with(door, "POISONED", confirm="maybe")
        assert "respawn-pane" not in log

    def test_the_prompt_says_what_will_be_lost(self, door):
        """An informed yes needs the consequence stated, not just the question.

        `respawn-pane -k` kills the pane's process GROUP.
        """
        proc, _log = self._run_with(door, "POISONED", confirm="n")
        assert "ends whatever is in it" in proc.stdout, (
            f"the prompt did not state the consequence:\n{proc.stdout}"
        )

    def test_the_answer_comes_from_the_terminal_not_stdin(self, door):
        """A redirected stdin must never be read as the operator's answer.

        The door is reached through ssh, but a caller can still redirect stdin;
        consuming that as consent would rebuild a slot nobody agreed to. Here
        stdin says "y" and the terminal says nothing — the correct read gets
        the terminal's empty line and declines. MEASURED: dropping `</dev/tty`
        survived the whole suite before this test existed.
        """
        _proc, log = self._run_with(door, "POISONED", confirm="", stdin_text="y\n")
        assert "respawn-pane" not in log, f"took stdin as the answer:\n{log}"

    def test_capacity_denial_stands_the_rebuild_down_without_asking(self, door):
        """DENY is not a question for the operator.

        They can see whether the pane is in use; they cannot see whether a
        swapless box can afford another ~3GB.

        DENY is not reachable from either entrance today (`session_cap.decide()`
        returns it only for a non-operator origin, and the only door that
        reaches a rebuild is the SSH one). This still locks the arm on purpose:
        handling a subset of the gate's outcome space is exactly what made the
        previous version of this gate a silent no-op.
        """
        proc, log = self._run_with(door, "POISONED", heal_cap="DENY")
        assert "respawn-pane" not in log, f"rebuilt past a capacity denial:\n{log}"
        assert "Rebuild this slot?" not in proc.stdout, "asked a question it had already answered"
        assert "capacity gate declined" in proc.stdout

    def test_a_slot_that_came_alive_while_deciding_is_not_rebuilt(self, door):
        """THE gate that the operator confirm does not replace.

        A human takes unbounded time at the prompt, and in that time they can
        attach from another terminal and start claude by hand. The verdict is
        therefore retaken AFTER the answer, under the lock: third probe says
        ALIVE, so the yes is discarded. Without that re-check this is a SIGKILL
        of a live Claude Code session — the one failure this door must never
        cause.
        """
        _proc, log = self._run_with(door, "POISONED,POISONED,ALIVE", confirm="y")
        assert "respawn-pane" not in log, f"rebuilt a slot that came alive:\n{log}"

    def test_the_verdict_is_retaken_after_the_answer(self, door):
        """Counts probes, so it cannot pass on the outcome alone."""
        run, _log, _sessions, _listing, _panes = door
        _proc, log = self._run_with(door, "POISONED", confirm="y")
        assert "respawn-pane" in log
        order = [x for x in run.probe_log.read_text().split() if x == "liveness"]
        assert len(order) == 3, (
            f"expected decision + pre-prompt + post-answer probes, got {order}"
        )

    def test_ctrl_c_at_the_prompt_never_rebuilds(self, door):
        """Ctrl-C aborts the door and rebuilds NOTHING.

        The safety property is the one that matters and is the one asserted:
        an interrupted prompt must never be read as consent. The abort itself
        matches `_cap_reclaim`, whose cancel path also ends the door. Trapping
        SIGINT to keep the door alive was tried and measured worse — bash
        restarts the interrupted `read`, so the door hung with the prompt still
        up instead of ending.

        The signal is sent only AFTER the prompt appears: written earlier it is
        buffered as ordinary input and never becomes SIGINT at all, which made
        an earlier version of this test pass with no signal handling present.
        """
        proc, log = self._run_with(
            door, "POISONED", confirm="\x03", wait_for="Rebuild this slot?",
        )
        assert "respawn-pane" not in log, f"Ctrl-C rebuilt the slot:\n{log}"
        assert "Rebuild this slot?" in proc.stdout, "the prompt never appeared"

    def test_an_answer_given_for_one_pane_does_not_destroy_another(self, door):
        """Consent is for a STATE, not for a slot.

        The prompt says the pane is running X and the operator agrees. If by
        the time the lock is taken it is running Y, that yes was never given
        for Y. Re-checking only pane IDENTITY and absence-of-claude would let a
        yes given for an idle `bash` destroy an editor started during exactly
        the window the prompt's own 120s timeout allows for.
        """
        proc, log = self._run_with(
            door, "POISONED", confirm="y",
            panes_text="%0 4242 bash\n",
            panes3_text="%0 4242 vim\n",
        )
        assert "respawn-pane" not in log, f"destroyed a pane it never disclosed:\n{log}"
        assert "not 'bash' as shown" in proc.stdout

    def test_an_oom_floor_reclaim_is_refused_rather_than_offered(self, door):
        """RECLAIM has two causes and only one of them is a choice.

        `cap_full` can be traded away by ending a session. `oom_floor` means
        RAM is below the safety floor, where starting a process risks an OOM
        that takes down every session — the create path refuses there too.
        """
        proc, log = self._run_with(door, "POISONED", heal_cap="RECLAIM|oom_floor")
        assert "respawn-pane" not in log, f"rebuilt below the OOM floor:\n{log}"
        assert "Rebuild this slot?" not in proc.stdout, "offered a choice it should not have"

    def test_a_reclaim_stands_the_rebuild_down(self, door):
        """RECLAIM means "not without ending something else" — never a warning.

        The CREATE path this rebuild is modelled on does not proceed on a
        RECLAIM either; it runs the interactive reclaim flow. Treating it as a
        note made the rebuild strictly MORE permissive than the create it
        imitates, growing the live population past the configured emergency
        limit — exactly what the cap governs. Covers BOTH causes.
        """
        for reason in ("cap_full", "oom_floor"):
            proc, log = self._run_with(
                door, "POISONED", heal_cap=f"RECLAIM|{reason}", confirm="y",
            )
            assert "respawn-pane" not in log, f"rebuilt past a RECLAIM ({reason}):\n{log}"
            assert "cap message" in proc.stdout, (
                f"did not use the gate's own wording ({reason}):\n{proc.stdout}"
            )

    def test_the_rebuild_takes_the_per_slot_lock(self, door):
        """Pins that the lock is TAKEN, not merely released correctly.

        MEASURED: deleting `_acquire_heal_lock` outright left the whole suite
        green, because the only lock test asserted a NEGATIVE — that fd 9 is
        not inherited — which is trivially true when no lock is ever opened.
        """
        run, _log, _sessions, _listing, _panes = door
        _proc, log = self._run_with(door, "POISONED", confirm="y")
        assert "respawn-pane" in log
        locks = list((run.fds_file.parent / "home" / ".genesis").glob("cc-slot-heal-*.lock"))
        assert locks, "no per-slot lock was created — the flock is not being taken"

    def test_the_capacity_question_excludes_this_slot_and_never_goes_negative(self, door):
        """`--existing` is the population the gate judges, and it must not go
        negative.

        A rebuild is create-like for memory, and a create's count EXCLUDES the
        session about to exist, so the rebuild asks about one fewer. With no
        sessions listed the subtraction must clamp: a prior round caught
        `--existing -1` reaching the gate as a nonsense population, and nothing
        pinned it afterwards.
        """
        run, _log, _sessions, listing, _panes = door
        listing.write_text("")
        self._run_with(door, "POISONED", heal_cap="ALLOW|ok", confirm="y")
        probe = run.probe_log.read_text()
        assert "--existing -" not in probe, f"passed a negative population:\n{probe}"
        assert "--existing 0" in probe, f"did not ask about one fewer session:\n{probe}"

    def test_eof_at_the_prompt_declines_instead_of_aborting(self, door):
        """Ctrl-D makes `read` fail; the door must DECLINE, not die.

        MEASURED: dropping `|| _heal_confirm=""` left the suite green, yet
        under `set -euo pipefail` a failed read aborts the script — the
        operator would get no rebuild, no message, and no shell. The declined
        message below can only appear if the door survived the failed read.
        """
        proc, log = self._run_with(
            door, "POISONED", confirm="\x04", wait_for="Rebuild this slot?",
        )
        assert "respawn-pane" not in log, f"EOF was read as consent:\n{log}"
        assert "left as-is" in proc.stdout, (
            f"a failed read aborted the door instead of declining:\n{proc.stdout}"
        )

    def test_no_capacity_verdict_stands_the_rebuild_down(self, door):
        """Fail CLOSED when the gate cannot answer.

        "Fail open like the create path" was the earlier reasoning and it was
        wrong: create does not fail open, it falls through to `_cap_fail_open`,
        which still enforces a MemTotal-derived cap and the hard OOM floor. A
        rebuild that simply proceeded was therefore WEAKER than a create on a
        degraded Python environment — it could start the measured ~3GB process
        on a swapless box where a create would refuse. Standing down costs one
        manual repair; the other direction costs an OOM that takes every
        session with it.
        """
        proc, log = self._run_with(door, "POISONED", heal_cap="")
        assert "respawn-pane" not in log, f"rebuilt with no capacity verdict:\n{log}"
        assert "Rebuild this slot?" not in proc.stdout, "asked before it could answer"
        assert "no usable verdict" in proc.stdout

    def test_capacity_is_re_derived_after_the_answer(self, door):
        """Dependency 5 of the enumerated set, and the last one to be closed.

        The capacity verdict shown to the operator can be 120 seconds old by
        the time they answer, and the session count behind it older still.
        Another slot can start, or free memory fall, inside that window — and a
        stale ALLOW would put the measured ~3GB process below the current floor
        on a swapless box. Here the gate says ALLOW while deciding and DENY by
        the time it matters; the yes must not survive that.
        """
        _proc, log = self._run_with(
            door, "POISONED", confirm="y", heal_cap="ALLOW|ok;DENY|cap_full",
        )
        assert "respawn-pane" not in log, f"acted on a stale capacity verdict:\n{log}"

    def test_a_vanished_session_is_not_silently_created(self, door):
        """Dependency 4, and it PRE-DATES the rebuild.

        `-A` creates when the session is gone, and `_SESSION_EXISTS` is latched
        near the top of the run — so a session that disappears during the
        probes, the OAuth gate, the lock or the prompt turns an attach into a
        create that traversed none of the create gates. The door must refuse
        and send the operator back through the front door instead.
        """
        proc, log = self._run_with(door, "POISONED", confirm="y", vanish=2)
        assert "new-session" not in log, f"created a session past the create gates:\n{log}"
        assert proc.returncode != 0, "exited 0 on a race it did not handle"
        assert "disappeared while this door was preparing" in proc.stdout

    def test_an_unacquired_lock_stands_the_rebuild_down(self, door):
        """Dependency 7 — which my own enumeration claimed was already closed.

        The lock was best-effort: a `flock -w 45` TIMEOUT was discarded, and a
        missing flock or unwritable path continued silently. Two confirmed
        doors could then both finish revalidation before either acted, and the
        second `respawn-pane -k` would kill the claude the first just started.
        Best-effort is a fine posture for a lock that ORDERS work; it is not
        one for a lock that prevents a kill.
        """
        proc, log = self._run_with(door, "POISONED", confirm="y", flock_fail=True)
        assert "respawn-pane" not in log, f"rebuilt without holding the lock:\n{log}"
        assert "could not take the per-slot rebuild lock" in proc.stdout

    def test_the_oauth_lever_reaches_the_pane(self, door):
        """The lever is a NON-exported shell var from ~/.genesis/cc-slot.env.

        Without passing it explicitly the in-tmux wrapper cannot see it and
        defaults to `conditional`, ignoring both an explicit `always` and the
        `off` kill switch — so a hand relaunch inside the slot could inject a
        token the operator had deliberately disabled.
        """
        _proc, log = self._run_with(door, "POISONED", confirm="y")
        line = [ln for ln in log.splitlines() if "new-session" in ln]
        assert line and "GENESIS_CC_SLOT_OAUTH=" in line[0], (
            f"the OAuth lever never reaches the pane:\n{line}"
        )

    def test_confirming_rebuilds_the_slot(self, door):
        _proc, log = self._run_with(door, "POISONED", confirm="y")
        assert "respawn-pane" in log


class TestWrapperInsideTmux:
    """A hand-typed `claude` inside a slot pane must not be a second-class
    session. Before this branch the wrapper fell through to a bare
    `command claude`, losing the permission flag (so the operator typed it by
    hand), the temp dirs CC expects, and the exit capture — which is precisely
    the state that makes the NEXT crash undiagnosable.

    Behavioural, not textual: the block is extracted from bootstrap.sh and
    sourced against a fake `claude` that records its argv and environment.
    """

    @staticmethod
    def _harness(tmp_path, env_extra, bootstrap_text, args='', oauth=False):
        block = bootstrap_text
        start = block.index("# >>> genesis tmux-wrap >>>")
        end = block.index("# <<< genesis tmux-wrap <<<")
        wrapper = block[start:end]

        binv = tmp_path / "bin"
        binv.mkdir(exist_ok=True)
        rec = tmp_path / "rec.txt"
        fake = binv / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'{{ echo "ARGV=$*"; echo "TMPDIR=$TMPDIR"; '
            f'echo "CCTMP=$CLAUDE_CODE_TMPDIR"; '
            f'echo "OAUTH=${{CLAUDE_CODE_OAUTH_TOKEN:-none}}"; }} >> {str(rec)!r}\n'
            "exit 7\n"
        )
        fake.chmod(0o755)
        home = tmp_path / "home"
        (home / "genesis" / "scripts").mkdir(parents=True, exist_ok=True)
        cap = home / "genesis" / "scripts" / "cc_exit_capture.sh"
        cap.write_text(f'#!/usr/bin/env bash\necho "CAPTURE=$1 $2" >> {str(rec)!r}\n')
        cap.chmod(0o755)
        if oauth:
            vbin = home / "genesis" / ".venv" / "bin"
            vbin.mkdir(parents=True, exist_ok=True)
            py = vbin / "python"
            # Stands in for BOTH halves the door uses: the gate that DECIDES
            # (`-m genesis.cc.login_gate`, whose stdout is the operator notice)
            # and the parser that READS the token (`-c ...`).
            py.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *login_gate*)\n'
                '     if [[ -n "${FAKE_GATE_DECLINES:-}" ]]; then\n'
                '       echo "run: claude setup-token (the stored one is stale)" >&2\n'
                '       exit 1\n'
                '     fi\n'
                '     echo "using the stored fallback login"; exit 0 ;;\n'
                '  *read_fallback_token*) printf %s "sk-fake-fallback"; exit 0 ;;\n'
                "esac\nexit 1\n"
            )
            py.chmod(0o755)

        script = f"""
set -u
unset TMPDIR CLAUDE_CODE_TMPDIR GENESIS_CC_PERMISSION_MODE GENESIS_SLOT GENESIS_NO_TMUX_WRAP
export PATH={str(binv)!r}:/usr/bin:/bin
export HOME={str(home)!r}
{env_extra}
{wrapper}
claude {args}
echo "RC=$?" >> {str(rec)!r}
"""
        # A REAL pty: the wrapper gates on `-t 0 && -t 1` so that a piped or
        # scripted `claude` is never rewritten. Under capture_output those are
        # pipes, the branch is skipped, and the test would silently measure the
        # fall-through instead of the code under test.
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child exec's away
            # Deliberate: the pty child must BECOME the shell so the
            # wrapper sees a real tty on fd 0/1. Test-only, fixed argv.
            os.execvp("bash", ["bash", "-c", script])  # noqa: S606
        seen: list[bytes] = []
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                seen.append(chunk)
            else:  # pragma: no cover - only on a hang
                os.kill(pid, 9)
                os.waitpid(pid, 0)
        finally:
            os.close(fd)
            # Reap unconditionally: the read loop can exit before the child does.
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        return (b"".join(seen).decode(errors="replace"),
                rec.read_text() if rec.exists() else "")

    def test_a_hand_relaunch_gets_the_slot_fallback_login(self, tmp_path):
        """The path this feature makes likely must not be auth-degraded.

        An operator declines the rebuild to keep their shell, then types
        `claude` themselves. Before this, that was the ONE launch that could not
        use a configured fallback token — unlike both the create and the
        confirmed-rebuild paths — so a valid token on disk could still strand
        them at authentication. It now runs the same `login_gate` and the same
        token parser the door uses, not a second implementation of either.
        """
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7',
            bootstrap_text=text, oauth=True,
        )
        assert "OAUTH=sk-fake-fallback" in rec, (
            f"the hand relaunch did not receive the slot's fallback token:\n{rec}"
        )

    def test_the_oauth_lever_still_turns_the_fallback_off(self, tmp_path):
        """`GENESIS_CC_SLOT_OAUTH=off` is an operator lever, not a suggestion."""
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path,
            'export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7\n'
            'export GENESIS_CC_SLOT_OAUTH=off',
            bootstrap_text=text, oauth=True,
        )
        assert "OAUTH=none" in rec, f"honoured a token despite the off lever:\n{rec}"

    def test_bare_skips_the_fallback_entirely(self, tmp_path):
        """`claude --bare` IGNORES CLAUDE_CODE_OAUTH_TOKEN.

        Probing for one anyway is worse than useless: it exports an inert token
        and announces the session is on the stored fallback when it is not,
        misleading exactly the person trying to diagnose their authentication.
        The canonical launcher skips the gate for --bare; this path must match.
        """
        text = Path(_BOOTSTRAP).read_text()
        _out, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7',
            bootstrap_text=text, args='--bare', oauth=True,
        )
        assert "OAUTH=none" in rec, f"probed and exported a token --bare ignores:\n{rec}"

    def test_the_gates_own_diagnostic_reaches_the_operator(self, tmp_path):
        """When the gate DECLINES it prints the fix; swallowing that leaves the
        operator on ordinary auth with no explanation of why.

        MEASURED before: the wrapper sent the gate's stderr to /dev/null, so
        `always` with a stale token fell back silently.
        """
        text = Path(_BOOTSTRAP).read_text()
        out, rec = self._harness(
            tmp_path,
            'export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7\n'
            'export FAKE_GATE_DECLINES=1',
            bootstrap_text=text, oauth=True,
        )
        assert "claude setup-token" in out, (
            f"the gate's operator diagnostic was swallowed:\n{out}"
        )
        assert "OAUTH=none" in rec, "injected a token the gate declined"

    def test_temp_dir_falls_back_when_the_slot_dir_cannot_be_made(self, tmp_path):
        """The fallback has to NAME a different directory, not "the ambient one".

        This branch runs only inside a slot pane, where the launcher has
        ALREADY exported TMPDIR to the directory whose creation is now failing.
        So "start Claude with the ambient TMPDIR" would hand it exactly the
        missing path — the failure it was trying to avoid, moved somewhere less
        visible. The env_extra below reproduces that precondition deliberately;
        the harness's usual `unset` would hide it.
        """
        text = Path(_BOOTSTRAP).read_text()
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        # A FILE where the directory needs to be: `mkdir -p` cannot succeed.
        (home / ".genesis").write_text("not a directory\n")
        bad = home / ".genesis" / "cc-tmp"
        _unused, rec = self._harness(
            tmp_path,
            "export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7\n"
            f"export TMPDIR={str(bad)!r}\nexport CLAUDE_CODE_TMPDIR={str(bad)!r}",
            bootstrap_text=text,
        )
        assert f"TMPDIR={home}/tmp" in rec, (
            f"did not fall back to a usable temp dir:\n{rec}"
        )
        assert str(bad) not in rec, (
            f"started Claude pointed at the directory that could not be created:\n{rec}"
        )

    def test_injects_permission_flag_tmpdirs_and_capture(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7',
            bootstrap_text=text,
        )
        assert "--permission-mode auto" in rec, rec
        assert "cc-tmp" in rec, f"temp dirs not applied:\n{rec}"
        assert "CAPTURE=7 7" in rec, f"exit capture not invoked with slot+code:\n{rec}"
        assert "RC=7" in rec, f"claude's exit code not propagated:\n{rec}"

    def test_caller_supplied_flag_is_not_doubled(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0', args='--permission-mode plan',
            bootstrap_text=text,
        )
        argv = [ln for ln in rec.splitlines() if ln.startswith("ARGV=")][0]
        assert "--permission-mode plan" in argv
        assert "auto" not in argv, f"two conflicting permission flags:\n{argv}"

    def test_missing_slot_env_falls_back_to_a_label(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(tmp_path, 'export TMUX=/tmp/fake,1,0', bootstrap_text=text)
        assert "CAPTURE=manual 7" in rec, f"no graceful slot label:\n{rec}"

    def test_opt_out_bypasses_the_branch_entirely(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0\nexport GENESIS_NO_TMUX_WRAP=1',
            bootstrap_text=text,
        )
        assert "RC=7" in rec, f"harness never reached the fake claude:\n{rec}"
        assert "--permission-mode" not in rec, f"opt-out ignored:\n{rec}"
        assert "CAPTURE=" not in rec, f"opt-out still ran exit capture:\n{rec}"

    def test_print_mode_still_passes_straight_through(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0', args='-p hello', bootstrap_text=text,
        )
        assert "RC=7" in rec, f"harness never reached the fake claude:\n{rec}"
        assert "--permission-mode" not in rec, f"headless call was wrapped:\n{rec}"
