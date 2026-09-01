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
    [[ -f "$FAKE_TMUX_SESSIONS" ]] && grep -qxF "$name" "$FAKE_TMUX_SESSIONS" && exit 0
    exit 1
fi
if [[ "$args" == *list-panes* ]]; then
    # 'pane_id pane_pid' rows, one per pane. Absent file -> no output, which
    # exercises the probe's "cannot enumerate" path.
    [[ -f "$FAKE_TMUX_PANES" ]] && cat "$FAKE_TMUX_PANES"
    exit 0
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
        # Any other module (the capacity gate, the login gate) -> no verdict,
        # which those paths already treat as unavailable.
        "exit 1\n"
    )
    fake_py.chmod(fake_py.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "tmux.log"
    sessions = tmp_path / "sessions.txt"
    listing = tmp_path / "list.txt"
    panes = tmp_path / "panes.txt"

    def run(*args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_TMUX_LOG": str(log),
            "FAKE_TMUX_SESSIONS": str(sessions),
            "FAKE_TMUX_LIST": str(listing),
            "FAKE_TMUX_PANES": str(panes),
            "FAKE_LIVENESS": os.environ.get("_TEST_FAKE_LIVENESS", ""),
            "FAKE_LIVENESS_N": str(tmp_path / "liveness_calls.txt"),
        }
        # Deliberately NOT inheriting os.environ: the test itself may run
        # inside a cc slot, whose GENESIS_CC_PERMISSION_MODE / TMUX would
        # contaminate the branch under test.
        return subprocess.run(
            ["bash", str(_CC_SLOT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

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

    def _run_with(self, door, verdict, panes_text="%0 4242 bash\n", session="cc-4"):
        run, log, sessions, _listing, panes = door
        sessions.write_text(f"{session}\n")
        panes.write_text(panes_text)
        os.environ["_TEST_FAKE_LIVENESS"] = verdict
        try:
            proc = run(f"genesis-3-{session.split('-')[1]}")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        return proc, log.read_text()

    def test_poisoned_slot_is_healed_with_the_full_launch_line(self, door):
        proc, log = self._run_with(door, "POISONED")
        assert "send-keys" in log, f"no heal attempted:\n{log}"
        literal = [ln for ln in log.splitlines() if "send-keys" in ln and " -l " in ln]
        assert literal, f"no literal payload sent:\n{log}"
        payload = literal[0]
        # The healed pane must run the SAME thing a fresh slot runs: cd guard,
        # permission flag, exit-capture trailer, exit-code preservation.
        for fragment in ("cd ", "claude ", "--permission-mode auto",
                         "cc_exit_capture.sh 4", "; __ec=", "exit $__ec"):
            assert fragment in payload, f"missing {fragment!r} in heal payload:\n{payload}"

    def test_heal_clears_the_prompt_before_typing(self, door):
        _proc, log = self._run_with(door, "POISONED")
        keys = [ln for ln in log.splitlines() if "send-keys" in ln]
        joined = "\n".join(keys)
        assert "C-c" in joined, "stray foreground process not interrupted"
        assert "C-u" in joined, "prompt residue not cleared"
        assert keys[-1].endswith("Enter"), f"payload never submitted:\n{joined}"

    def test_heal_prints_an_honest_message(self, door):
        proc, _log = self._run_with(door, "POISONED")
        assert "running no claude" in proc.stderr, proc.stderr

    def test_alive_slot_is_attached_untouched(self, door):
        proc, log = self._run_with(door, "ALIVE")
        assert "send-keys" not in log, f"typed into a LIVE session:\n{log}"
        assert "running no claude" not in proc.stderr

    def test_unknown_verdict_spares_the_session(self, door):
        _proc, log = self._run_with(door, "UNKNOWN")
        assert "send-keys" not in log, f"typed into a session of unknown state:\n{log}"

    def test_empty_verdict_spares_the_session(self, door):
        # Broken venv / timeout / crashed probe -> no verdict at all.
        _proc, log = self._run_with(door, "")
        assert "send-keys" not in log, f"typed on an absent verdict:\n{log}"

    def test_garbage_verdict_spares_the_session(self, door):
        _proc, log = self._run_with(door, "banana")
        assert "send-keys" not in log

    def test_unenumerable_panes_spares_the_session(self, door):
        _proc, log = self._run_with(door, "POISONED", panes_text="")
        assert "send-keys" not in log, f"healed without knowing which pane:\n{log}"

    def test_heal_targets_the_first_windows_pane(self, door):
        # list-panes -s lists in window order, so row 1 is window 1's pane —
        # where the canonical launch lives.
        _proc, log = self._run_with(door, "POISONED", panes_text="%5 111 bash\n%7 222 bash\n")
        sk = [ln for ln in log.splitlines() if "send-keys" in ln]
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
        assert "send-keys" in log

    def test_alive_attach_still_skips_the_oauth_gate(self, door):
        # An attach does not re-run the pane command, so injecting a token
        # there would be inert and would waste a probe.
        _proc, log = self._run_with(door, "ALIVE")
        assert "send-keys" not in log

    def test_heal_does_not_silence_the_doors_own_stderr(self, door):
        """Regression: the per-slot lock is opened with `exec`, whose
        redirections apply to the SHELL. A `2>/dev/null` on that line silenced
        every later notice for the rest of the run — the heal still happened,
        so send-keys assertions stayed green while the operator was told
        nothing. Assert a message that is emitted AFTER the lock is taken.
        """
        proc, log = self._run_with(door, "POISONED")
        assert "send-keys" in log
        assert proc.stderr.strip(), "the door went silent after taking the lock"
        assert "relaunching" in proc.stderr, proc.stderr

    def test_still_exactly_one_new_session_on_the_heal_path(self, door):
        _proc, log = self._run_with(door, "POISONED")
        assert len([ln for ln in log.splitlines() if "new-session" in ln]) == 1

    def test_pane_running_a_foreground_job_is_never_touched(self, door):
        """Liveness answers "is claude running", NOT "is it safe to type here".

        MEASURED: send-keys C-c KILLS a running foreground job. A slot with no
        claude but an active build, ssh session or editor must be left strictly
        alone — including the C-c, which is itself destructive.
        """
        _proc, log = self._run_with(door, "POISONED", panes_text="%0 4242 vim\n")
        assert "send-keys" not in log, f"typed into a pane running vim:\n{log}"

    @pytest.mark.parametrize("busy", ["vim", "ssh", "python", "pytest", "less", "node"])
    def test_non_shell_panes_are_all_spared(self, door, busy):
        _proc, log = self._run_with(door, "POISONED", panes_text=f"%0 4242 {busy}\n")
        assert "send-keys" not in log, f"typed into a pane running {busy}:\n{log}"

    def test_busy_pane_stand_down_is_announced(self, door):
        proc, _log = self._run_with(door, "POISONED", panes_text="%0 4242 rsync\n")
        assert "running 'rsync'" in proc.stderr, proc.stderr

    @pytest.mark.parametrize("shell", ["bash", "-bash", "sh", "zsh", "dash", "fish"])
    def test_shell_panes_still_heal(self, door, shell):
        # Login shells arrive with a leading dash; missing them would make the
        # gate refuse to heal the exact shape that motivated this change.
        _proc, log = self._run_with(door, "POISONED", panes_text=f"%0 4242 {shell}\n")
        assert "send-keys" in log, f"refused to heal an idle {shell} pane:\n{log}"

    def test_malformed_pane_id_is_never_a_send_keys_target(self, door):
        _proc, log = self._run_with(door, "POISONED", panes_text="garbage 4242 bash\n")
        assert "send-keys" not in log, f"typed at a non-pane target:\n{log}"

    def test_slot_that_came_alive_during_preparation_is_not_typed_into(self, door):
        """The verdict is re-confirmed immediately before the keystrokes.

        Up to ~30s elapses between the decision and send-keys (the OAuth gate
        sits in between), which is ample for a second door — or the operator in
        another terminal — to have started claude in this very pane. Typing a
        command line into a live Claude Code TUI is the one failure this change
        must never cause, so a stale POISONED must not be acted on.
        """
        _proc, log = self._run_with(door, "POISONED,ALIVE")
        assert "send-keys" not in log, f"typed into a slot that came alive:\n{log}"

    def test_race_stand_down_is_announced(self, door):
        proc, _log = self._run_with(door, "POISONED,ALIVE")
        assert "came alive while preparing" in proc.stderr, proc.stderr

    def test_still_heals_when_the_recheck_agrees(self, door):
        _proc, log = self._run_with(door, "POISONED,POISONED")
        assert "send-keys" in log, f"re-check wrongly blocked a real heal:\n{log}"



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
    def _harness(tmp_path, env_extra, bootstrap_text, args=''):
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
            f'echo "CCTMP=$CLAUDE_CODE_TMPDIR"; }} >> {str(rec)!r}\n'
            "exit 7\n"
        )
        fake.chmod(0o755)
        home = tmp_path / "home"
        (home / "genesis" / "scripts").mkdir(parents=True, exist_ok=True)
        cap = home / "genesis" / "scripts" / "cc_exit_capture.sh"
        cap.write_text(f'#!/usr/bin/env bash\necho "CAPTURE=$1 $2" >> {str(rec)!r}\n')
        cap.chmod(0o755)

        script = f"""
set -u
unset TMPDIR CLAUDE_CODE_TMPDIR GENESIS_CC_PERMISSION_MODE
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
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                try:
                    if not os.read(fd, 4096):
                        break
                except OSError:
                    break
            else:  # pragma: no cover - only on a hang
                os.kill(pid, 9)
                os.waitpid(pid, 0)
        finally:
            os.close(fd)
            # Reap unconditionally: the read loop can exit before the child does.
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        return None, (rec.read_text() if rec.exists() else "")

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
