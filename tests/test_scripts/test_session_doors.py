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
# Record the TMPDIR this process INHERITED, distinguishing unset from empty.
# `exec tmux` is what starts the tmux SERVER when none is running, so an empty
# exported TMPDIR here would be inherited by that server and by every slot
# created on it afterwards — invisible on the argv line the other assertions read.
echo "INHERITED_TMPDIR=[${TMPDIR-<unset>}]" >> "$FAKE_TMUX_LOG"
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
            "FAKE_TMUX_LIST_PANES_FAIL": os.environ.get(
                "_TEST_FAKE_LIST_PANES_FAIL", ""),
        }
        # Only set when a test asks: the door must be exercised with a REAL
        # inherited TMPDIR to see what it passes on, and an unconditional entry
        # would change every other test's environment.
        _inherited = os.environ.get("_TEST_INHERITED_TMPDIR")
        if _inherited:
            env["TMPDIR"] = _inherited
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

    run.home = home  # so a test can make the temp-dir candidates unusable
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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bit")
    def test_no_usable_temp_dir_leaves_tmpdir_unset_not_empty(self, door, tmp_path):
        """When NO candidate is usable, the door must leave TMPDIR genuinely
        unset — never exported-but-empty.

        `TMPDIR=""` on an already-exported variable KEEPS the export attribute,
        so the child receives a literal `TMPDIR=`. This script ends in
        `exec tmux`, which STARTS the server when none is running, so an empty
        value is inherited by that server and — per the measured note at the
        exec — by every slot created on it afterwards. It would also make the
        "leaving CC on the system default" message false, and it bypasses the
        conditional `-e` pin through the ambient environment rather than the
        argv line the other assertions read.
        """
        run, log, _sessions, _listing, _panes = door
        home = Path(run.home)
        for cand in (home / ".genesis" / "cc-tmp", home / "tmp"):
            cand.mkdir(parents=True, exist_ok=True)
            cand.chmod(0o500)  # exists, ours, but chmod 700 cannot be applied...
        os.environ["_TEST_INHERITED_TMPDIR"] = "/inherited/from/parent"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_INHERITED_TMPDIR", None)
            for cand in (home / ".genesis" / "cc-tmp", home / "tmp"):
                cand.chmod(0o700)  # so tmp_path teardown can clean up
        assert proc.returncode == 0, proc.stderr
        body = log.read_text()
        # Omission is not absence: with no `-e` pin the pane would take the tmux
        # SERVER's value, which may be the very directory just rejected. The pane
        # command must unset both names explicitly, joined with `&&` so a failed
        # `cd` still skips claude.
        assert "unset TMPDIR CLAUDE_CODE_TMPDIR &&" in body, (
            f"no -e pin AND no unset: the pane inherits the server's stale temp "
            f"dir, so 'system default' is false:\n{body}"
        )
        assert "INHERITED_TMPDIR=[<unset>]" in body, (
            "the door handed tmux an exported TMPDIR when none was usable; an "
            f"empty one poisons the server it starts:\n{body}\n{proc.stderr}"
        )
        assert "-e TMPDIR=" not in body, f"pinned an unusable TMPDIR:\n{body}"

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
        # A deadline is only real if EVERY wait under it is bounded. Both waits
        # here would otherwise block forever on a child that produces no output
        # and never exits (an auth regression waiting on input is the realistic
        # shape): a bare `os.read` blocks before the loop can re-check the
        # clock, and the reap in `finally` blocks on a live child — so the test
        # would hang the suite instead of failing in 30s. `select` bounds the
        # read by the SAME deadline, and the child is killed before the reap.
        deadline = time.monotonic() + 30
        try:
            while True:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # pragma: no cover - only on a hang
                    # pty.fork() returns the child's real pid in the parent, so
                    # this is never a pgid-0/1 broadcast.
                    os.kill(pid, 9)
                    break
                # Short slices so an exit with no further output is noticed
                # promptly by the WNOHANG check above rather than at the ceiling.
                ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                seen.append(chunk)
        finally:
            os.close(fd)
            # Reap unconditionally: the read loop can exit before the child does.
            # Safe to block here — either the child already exited, or the
            # deadline branch above SIGKILLed it, so this returns either way.
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

    def test_persisted_levers_are_read_from_the_config_file(self, tmp_path):
        """The wrapper must read ~/.genesis/cc-slot.env, as the launcher does.

        A pane created before the `-e` pins — or with plain tmux — carries none
        of the GENESIS_* levers, and those stale slots are exactly where a hand
        relaunch gets used. Reading only the pane environment there silently
        ignores a configured permission mode and OAuth lever, which is the
        opposite of the parity this branch exists to give.
        """
        text = Path(_BOOTSTRAP).read_text()
        home = tmp_path / "home"
        (home / ".genesis").mkdir(parents=True, exist_ok=True)
        (home / ".genesis" / "cc-slot.env").write_text(
            "GENESIS_CC_PERMISSION_MODE=bypass\nGENESIS_CC_SLOT_OAUTH=off\n"
        )
        # No GENESIS_* in the pane env at all — the legacy-slot shape.
        _unused, rec = self._harness(
            tmp_path, "export TMUX=/tmp/fake,1,0", bootstrap_text=text, oauth=True
        )
        assert "--dangerously-skip-permissions" in rec, (
            f"ignored the configured permission mode:\n{rec}"
        )
        assert "--permission-mode auto" not in rec, rec
        assert "OAUTH=none" in rec, (
            f"injected a token despite the configured off lever:\n{rec}"
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

        In a slot pane the launcher has ALREADY exported TMPDIR to the very
        directory whose creation is now failing (the branch itself fires in any
        interactive tmux, but that is the case the ambient value cannot rescue).
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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bit")
    def test_an_existing_unwritable_slot_dir_is_rejected_not_used(self, tmp_path):
        """`mkdir -p` SUCCEEDS on a directory that already exists — including one
        we cannot write (a root-owned leftover from an earlier sudo run). Creation
        is not usability; only the writability check separates them, and without
        it Claude launches pointed at a temp dir whose every write fails INSIDE
        CC instead of here.

        The existing fallback test cannot cover this: it makes `mkdir -p` itself
        fail, so it passes with the writability check deleted.
        """
        text = Path(_BOOTSTRAP).read_text()
        home = tmp_path / "home"
        bad = home / ".genesis" / "cc-tmp"
        bad.mkdir(parents=True)
        bad.chmod(0o500)  # exists; mkdir -p returns 0; not writable
        try:
            _unused, rec = self._harness(
                tmp_path,
                "export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7",
                bootstrap_text=text,
            )
        finally:
            bad.chmod(0o700)  # so tmp_path teardown can remove it
        assert f"TMPDIR={home}/tmp" in rec, (
            f"did not fall back off an unwritable slot dir:\n{rec}"
        )
        assert f"TMPDIR={bad}" not in rec, (
            f"launched Claude on a temp dir it cannot write:\n{rec}"
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

    def test_setup_token_is_never_wrapped(self, tmp_path):
        """`claude setup-token` PRINTS a long-lived credential to the terminal.

        Wrapping it would run the exit capture, which appends a scrollback tail
        to a log file — writing that credential to disk. It must pass straight
        through, with no permission flag and no capture. The same applies to
        every other subcommand: none is an interactive session.
        """
        text = Path(_BOOTSTRAP).read_text()
        for sub in ("setup-token", "mcp", "doctor", "update"):
            sub_dir = tmp_path / sub
            sub_dir.mkdir()
            _unused, rec = self._harness(
                sub_dir,
                "export TMUX=/tmp/fake,1,0\nexport GENESIS_SLOT=7",
                bootstrap_text=text,
                args=sub,
            )
            assert "RC=7" in rec, f"{sub}: harness never reached claude:\n{rec}"
            assert "--permission-mode" not in rec, (
                f"{sub}: a subcommand was given a permission flag:\n{rec}"
            )
            assert "CAPTURE=" not in rec, (
                f"{sub}: exit capture ran for a subcommand — for setup-token that "
                f"writes a credential to a log:\n{rec}"
            )

    def test_print_mode_still_passes_straight_through(self, tmp_path):
        text = Path(_BOOTSTRAP).read_text()
        _unused, rec = self._harness(
            tmp_path, 'export TMUX=/tmp/fake,1,0', args='-p hello', bootstrap_text=text,
        )
        assert "RC=7" in rec, f"harness never reached the fake claude:\n{rec}"
        assert "--permission-mode" not in rec, f"headless call was wrapped:\n{rec}"
