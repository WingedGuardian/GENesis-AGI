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

import os
import stat
import subprocess
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
