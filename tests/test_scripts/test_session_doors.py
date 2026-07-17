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
    log = tmp_path / "tmux.log"
    sessions = tmp_path / "sessions.txt"
    listing = tmp_path / "list.txt"

    def run(*args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_TMUX_LOG": str(log),
            "FAKE_TMUX_SESSIONS": str(sessions),
            "FAKE_TMUX_LIST": str(listing),
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

    return run, log, sessions, listing


def _new_session_line(log: Path) -> str:
    lines = [ln for ln in log.read_text().splitlines() if "new-session" in ln]
    assert len(lines) == 1, f"expected exactly one new-session call, got: {lines}"
    return lines[0]


class TestManualMode:
    def test_first_free_slot_is_cc_1(self, door):
        run, log, _sessions, _listing = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "-A" in line
        assert "-s cc-1" in line
        assert "GENESIS_SLOT=1" in line
        assert "--permission-mode auto" in line

    def test_allocation_skips_existing_sessions(self, door):
        run, log, sessions, listing = door
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
        run, log, sessions, _listing = door
        sessions.write_text("cc-1\n")
        run("manual")
        probes = [ln for ln in log.read_text().splitlines() if "has-session" in ln]
        assert probes, "allocation must probe has-session"
        # '=' prefix: without it tmux prefix-matches, so cc-1 reads as
        # existing whenever only cc-10 does.
        assert all("-t =cc-" in p for p in probes), probes

    def test_extra_args_are_forwarded_into_the_slot(self, door):
        run, log, _sessions, _listing = door
        result = run("manual", "--resume", "abc123")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--resume abc123" in line
        assert "--permission-mode auto" in line  # no perm flag supplied -> default kept

    def test_caller_permission_flag_suppresses_default(self, door):
        run, log, _sessions, _listing = door
        result = run("manual", "--dangerously-skip-permissions")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--dangerously-skip-permissions" in line
        assert "--permission-mode auto" not in line

    def test_permission_mode_equals_form_also_suppresses(self, door):
        run, log, _sessions, _listing = door
        result = run("manual", "--permission-mode=plan")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "--permission-mode=plan" in line
        assert "--permission-mode auto" not in line

    def test_retired_manual_sessions_do_not_consume_cap(self, door):
        # Legacy cc-manual-<ts>-<pid> sessions from the old wrapper must not
        # count toward the slot cap: manual allocation can only ever create
        # numeric cc-N, so counting strays would falsely lock installs out.
        run, log, sessions, listing = door
        sessions.write_text("cc-1\n")
        listing.write_text(
            "cc-1|1|Thu Jul 16 20:00:00 2026\n"
            "cc-manual-1784246386-1229981|0|Thu Jul 16 19:59:46 2026\n"
        )
        result = run("manual")
        assert result.returncode == 0, result.stderr
        assert "-s cc-2" in _new_session_line(log)
        # Only the numeric slot counts: cap line reads 1/<max>, not 2/<max>.
        assert "cap: 1/" in result.stderr

    def test_exotic_arg_survives_quoting(self, door):
        run, log, _sessions, _listing = door
        result = run("manual", "--resume", "a b'c")
        assert result.returncode == 0, result.stderr
        # %q-quoted through the tmux command string: the shell inside the
        # session must receive the original value as ONE argument.
        line = _new_session_line(log)
        cmd = line.split("LANG=", 1)[1].split(" ", 1)[1]
        parsed = subprocess.run(
            ["bash", "-c", f'set -- {cmd.split("claude", 1)[1]}; printf "%s\\n" "$@"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "a b'c" in parsed.stdout.splitlines()


class TestHostnameMode:
    def test_hostname_parses_trailing_slot(self, door):
        run, log, _sessions, _listing = door
        result = run("genesis-3-4")
        assert result.returncode == 0, result.stderr
        line = _new_session_line(log)
        assert "-s cc-4" in line
        assert "GENESIS_SLOT=4" in line

    def test_invalid_hostname_rejected(self, door):
        run, _log, _sessions, _listing = door
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
