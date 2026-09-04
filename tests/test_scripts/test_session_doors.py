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
if [[ "$args" == *list-panes* ]]; then
    # A real tmux exits 1 for a session that has gone away; the door must
    # survive that (set -euo pipefail). Absent panes file -> no output,
    # exercising the probe's "cannot enumerate" path.
    if [[ -n "${FAKE_TMUX_LIST_PANES_FAIL:-}" ]]; then exit 1; fi
    [[ -f "$FAKE_TMUX_PANES" ]] && cat "$FAKE_TMUX_PANES"
    exit 0
fi
exit 0
"""


# Fake venv python: cc-slot resolves GENESIS_ROOT from HOME and consults a venv
# python for the slot-map liveness verdict. Fake it so the map's annotation
# branch can be driven directly; the probe's own logic is unit-tested in
# tests/test_cc/test_slot_liveness.py.
_FAKE_VENV_PY = """#!/usr/bin/env bash
if [[ "$*" == *slot_liveness* ]]; then
  # Ordered call log (which probe ran, in which order) + an optional per-probe
  # sleep so the whole-map deadline is exercisable (else every fake probe
  # returns instantly and a per-probe vs shared budget are indistinguishable).
  [[ -n "${FAKE_PROBE_LOG:-}" ]] && echo liveness >> "$FAKE_PROBE_LOG"
  if [[ -n "${FAKE_PROBE_SLEEP:-}" ]]; then sleep "$FAKE_PROBE_SLEEP"; fi
  # FAKE_LIVENESS may be a comma-separated SEQUENCE; each call consumes the next
  # entry (last one repeats). Empty -> no verdict line (probe "unavailable").
  n=0; [[ -f "$FAKE_LIVENESS_N" ]] && n=$(cat "$FAKE_LIVENESS_N")
  IFS="," read -ra _v <<< "${FAKE_LIVENESS:-}"
  idx=$n; (( idx >= ${#_v[@]} )) && idx=$(( ${#_v[@]} - 1 ))
  echo "${_v[$idx]:-}"
  echo "note"
  echo $(( n + 1 )) > "$FAKE_LIVENESS_N"
  exit 0
fi
exit 1
"""


@pytest.fixture()
def door(tmp_path):
    """Run cc-slot.sh with a fake tmux + isolated HOME.

    Returns (run, log, sessions, listing, panes). The slot map's liveness
    verdict is driven by the ``_TEST_FAKE_LIVENESS`` env var (a comma-separated
    sequence consumed one entry per probe); ``panes`` is the file the fake
    ``tmux list-panes`` serves so ``_map_pids`` is non-empty.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(_FAKE_TMUX)
    fake_tmux.chmod(fake_tmux.stat().st_mode | stat.S_IEXEC)

    home = tmp_path / "home"
    home.mkdir()
    venv_bin = home / "genesis" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(_FAKE_VENV_PY)
    fake_py.chmod(fake_py.stat().st_mode | stat.S_IEXEC)

    log = tmp_path / "tmux.log"
    sessions = tmp_path / "sessions.txt"
    listing = tmp_path / "list.txt"
    panes = tmp_path / "panes.txt"
    probe_log = tmp_path / "probe_log.txt"

    def run(*args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_TMUX_LOG": str(log),
            "FAKE_TMUX_SESSIONS": str(sessions),
            "FAKE_TMUX_LIST": str(listing),
            "FAKE_TMUX_PANES": str(panes),
            "FAKE_LIVENESS_N": str(tmp_path / "liveness_calls.txt"),
            # Test-controlled knobs, forwarded from the test's os.environ so a
            # test can drive the verdict without changing the fixture signature.
            "FAKE_LIVENESS": os.environ.get("_TEST_FAKE_LIVENESS", ""),
            "FAKE_PROBE_SLEEP": os.environ.get("_TEST_FAKE_PROBE_SLEEP", ""),
            "FAKE_PROBE_LOG": str(probe_log),
            "FAKE_TMUX_LIST_PANES_FAIL": os.environ.get(
                "_TEST_FAKE_LIST_PANES_FAIL", ""),
        }
        # Deliberately NOT inheriting os.environ wholesale: the test itself may
        # run inside a cc slot, whose GENESIS_CC_PERMISSION_MODE / TMUX would
        # contaminate the branch under test.
        return subprocess.run(
            ["bash", str(_CC_SLOT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    run.probe_log = probe_log  # ordered log of which slot-map probes ran
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

    def test_create_pins_measured_env_before_lang(self, door):
        """A new session must carry the env vars MEASURED to come from the tmux
        SERVER (not this client) on an existing server, so a slot created after
        a foreign server is correct.

        MEASURED on tmux 3.4 (new session on a pre-existing server):
        TMPDIR and GENESIS_CC_SLOT_OAUTH resolve to the SERVER's value → pinned
        here; PATH resolves to the CLIENT's value → NO gap, deliberately NOT
        pinned. LANG stays the LAST -e flag (other tests split the create line
        on it).
        """
        run, log, _sessions, _listing, _panes = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "-e TMPDIR=" in line, line
        assert "CLAUDE_CODE_TMPDIR=" in line, line  # sibling still present
        assert "-e GENESIS_CC_SLOT_OAUTH=" in line, line  # wrapper-lever gap
        # PATH was measured to propagate from the client already — pinning it is
        # unnecessary, so the create line must NOT carry an explicit PATH= -e.
        assert "-e PATH=" not in line, f"PATH pinned despite no measured gap:\n{line}"
        assert line.index(" TMPDIR=") < line.index(" LANG="), line  # LANG last
        assert line.index("GENESIS_CC_SLOT_OAUTH=") < line.index(" LANG="), line

    def test_slot_map_marks_a_session_with_no_claude(self, door):
        """The printed map is the door's only honest surface for a slot the
        door itself will not heal: manual allocation never grabs an existing
        session, so the map must SAY a slot has no claude and point at the door
        rather than `tmux attach` (which reattaches to the bare prompt)."""
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("4242\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "no claude" in proc.stderr, (
            f"a claude-less slot was listed as if healthy:\n{proc.stderr}"
        )

    def test_slot_map_leaves_a_live_slot_unannotated(self, door):
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|1|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("4242\n")
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
        """No verdict must never render as a verdict."""
        run, _log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("4242\n")
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
        """A session can vanish between listing and inspecting it (the server
        shuts down when the last slot's claude exits). Under set -euo pipefail a
        `var=$(tmux ... | tr ...)` whose first component fails would abort the
        whole door; the `|| true` guard must keep the login alive."""
        run, log, sessions, listing, panes = door
        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("4242\n")
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


class TestSlotMapBudget:
    """The slot map is COSMETIC and must never be what makes a login feel slow."""

    def test_one_deadline_is_shared_across_probes(self, door):
        """A per-probe timeout is not a budget: N slots each finishing just
        under it still cost N x budget. Twelve slots at 1s each, with a shared
        ~6s deadline, must stop probing partway through — strictly fewer than
        twelve probes run."""
        run, _log, _sessions, listing, panes = door
        listing.write_text("".join(f"cc-{i}|0|ts\n" for i in range(1, 13)))
        panes.write_text("4242\n")  # non-empty so a probe is attempted per slot
        os.environ["_TEST_FAKE_PROBE_SLEEP"] = "1"
        try:
            result = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_PROBE_SLEEP", None)
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
