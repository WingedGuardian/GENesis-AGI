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
    if [[ "$args" == *session_id* ]]; then
        # The consent block's 5-field SNAPSHOT format. Served from its own
        # file so the slot-map probe (pane_pid format) keeps its fixture.
        # A second snapshot file, when present, is consumed from the SECOND
        # call on — that is how a test moves the world between the disclose
        # and the confirm (the projection compare must then stand down).
        n=0; [[ -f "${FAKE_TMUX_SNAP_N:-/nonexistent}" ]] && n=$(cat "$FAKE_TMUX_SNAP_N")
        echo $(( n + 1 )) > "${FAKE_TMUX_SNAP_N:-/dev/null}" 2>/dev/null || true
        if [[ "$n" -ge 1 && -f "${FAKE_TMUX_SNAP2:-/nonexistent}" ]]; then
            cat "$FAKE_TMUX_SNAP2"
        elif [[ -f "${FAKE_TMUX_SNAP:-/nonexistent}" ]]; then
            cat "$FAKE_TMUX_SNAP"
        fi
        exit 0
    fi
    [[ -f "$FAKE_TMUX_PANES" ]] && cat "$FAKE_TMUX_PANES"
    exit 0
fi
if [[ "$args" == *kill-session* ]]; then
    # Record the exact target so tests can assert kill-BY-ID, and let a test
    # inject a failure (the announce-and-attach path).
    prev=""; tgt=""
    for a in "$@"; do
        if [[ "$prev" == "-t" ]]; then tgt="$a"; fi
        prev="$a"
    done
    echo "$tgt" >> "${FAKE_TMUX_KILLLOG:-/dev/null}" 2>/dev/null || true
    exit "${FAKE_TMUX_KILL_RC:-0}"
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
    snap = tmp_path / "snap.txt"
    snap2 = tmp_path / "snap2.txt"
    killlog = tmp_path / "kill.log"

    def _env() -> dict:
        return {
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
            "FAKE_TMUX_SNAP": str(snap),
            "FAKE_TMUX_SNAP2": str(snap2),
            "FAKE_TMUX_SNAP_N": str(tmp_path / "snap_calls.txt"),
            "FAKE_TMUX_KILLLOG": str(killlog),
            "FAKE_TMUX_KILL_RC": os.environ.get("_TEST_FAKE_KILL_RC", "0"),
        }
    def run(*args: str) -> subprocess.CompletedProcess:
        env = _env()
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

    run.env_fn = _env  # so the pty runner builds the IDENTICAL environment
    run.probe_log = probe_log  # ordered log of which slot-map probes ran
    run.snap = snap  # consent-block snapshot 1 (5-field lines)
    run.snap2 = snap2  # snapshot served from the SECOND call on (TOCTOU tests)
    run.killlog = killlog  # exact kill-session targets, one per line
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

    def test_slot_map_marks_a_session_with_no_claude(self, door):
        """The map must SAY a slot has no claude and name an action that works.

        The action changed with the consent kill-and-recreate: the hostname
        door now detects a bare slot, discloses it, and rebuilds the pane with
        the slot's full environment on a yes — so the honest advice is to go
        BACK THROUGH THE DOOR. (While the door could not heal, this same test
        asserted the OPPOSITE direction — the note had to say "run 'claude'"
        and must NOT point at the door, because `new-session -A` silently
        re-attached to the bare shell. The inversion is deliberate and this
        docstring is its record.)
        """
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
        # The advice must point at the door that now actually rebuilds.
        assert "through this slot's door" in proc.stderr, (
            f"the note does not name the door rebuild:\n{proc.stderr}"
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
        run, log, _sessions, listing, panes = door
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
        # And once the budget is spent the map must stop calling tmux AT ALL.
        # Bounding each call without bounding the aggregate is not a ceiling:
        # every remaining slot would still pay a round-trip (plus the -k grace),
        # which is how a wedged server could outlast the declared budget.
        listings = [
            ln for ln in log.read_text().splitlines() if "list-panes" in ln
        ]
        assert len(listings) < 12, (
            f"list-panes ran for every slot ({len(listings)}); the deadline "
            f"bounds each call but not the map"
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


# ── Consent kill-and-recreate (the hostname door heals, with consent) ────────


_SNAP = "930000|$7|0|4242|bash\n"


def _poisoned_slot(door, snap: str = _SNAP):
    """Arrange cc-1 as an existing, bare (claude-less) slot for hostname mode."""
    run, log, sessions, _listing, _panes = door
    sessions.write_text("cc-1\n")
    run.snap.write_text(snap)
    return run, log


def _run_door_pty(run, mode: str, feed: bytes):
    """Run the door under a pty so the consent `read < /dev/tty` is reachable,
    with the EXACT environment the subprocess runner builds."""
    import pty
    import select

    env = run.env_fn()
    pid, fd = pty.fork()
    if pid == 0:  # child: pty session leader
        try:
            os.execve("/bin/bash", ["bash", str(_CC_SLOT), mode], env)  # noqa: S606
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
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


class TestConsentRebuild:
    """The consent kill sits ABOVE every latch, so each gate below reads
    post-kill reality on its only read — the staleness class that took the
    predecessor through 7 review rounds is closed by construction. Every
    failure direction here must land on ATTACH, never on a kill."""

    def test_no_tty_reports_but_never_kills(self, door):
        # A piped/dispatched entry has nobody to consent: detect-and-tell,
        # then the ordinary attach. The decliner keeps the manual route.
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED"
        try:
            proc = run("genesis-3-1")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "runs NO claude" in proc.stderr, proc.stderr
        assert "tmux attach -t cc-1" in proc.stderr  # the manual route survives
        assert not run.killlog.exists() or run.killlog.read_text() == ""
        assert "new-session" in log.read_text()  # attach still happened

    def test_yes_kills_by_id_then_falls_through_to_create(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        # Kill BY ID — the measured compare-and-swap — never by name.
        assert run.killlog.read_text().strip() == "$7", (
            f"kill target was not the session id:\n{run.killlog.read_text()!r}\n{out}"
        )
        assert "rebuilding it fresh" in out, out
        assert "new-session" in log.read_text()  # the untouched create path ran

    def test_default_no_leaves_the_slot_alone(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text() == ""
        assert "leaving cc-1 as it is" in out, out
        assert "new-session" in log.read_text()

    def test_projection_change_between_disclose_and_confirm_stands_down(self, door):
        # Consent was given for a STATE, not a slot: vim starting mid-prompt
        # changes the pane-command projection, and the yes must not kill it.
        run, log = _poisoned_slot(door)
        run.snap2.write_text("930000|$7|0|4242|vim\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text() == ""
        assert "changed while you decided" in out, out
        assert "new-session" in log.read_text()

    def test_server_generation_change_stands_down(self, door):
        # MEASURED motive: across server generations the id counter restarts
        # at $0, so a stale id CAN name an innocent session — only the
        # server-PID compare makes the kill safe. Same id, new server: no kill.
        run, log = _poisoned_slot(door)
        run.snap2.write_text("940000|$7|0|4242|bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text() == ""
        assert "changed while you decided" in out, out

    def test_claude_appearing_mid_prompt_stands_down(self, door):
        # The projection can be identical while a claude just started under
        # the same pane pid's tree — the liveness RE-probe is the last word.
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,ALIVE"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text() == ""
        assert "changed while you decided" in out, out

    def test_kill_failure_is_announced_and_attach_continues(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_KILL_RC"] = "1"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_KILL_RC", None)
        assert code == 0, out
        assert "could not end cc-1" in out, out
        assert "new-session" in log.read_text()  # -A absorbs the interleaving

    def test_alive_slot_never_sees_the_consent_path(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "ALIVE"
        try:
            proc = run("genesis-3-1")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "runs NO claude" not in proc.stderr
        assert not run.killlog.exists() or run.killlog.read_text() == ""

    def test_malformed_session_id_fails_toward_attach(self, door):
        # A snapshot whose id field is not `$N` (garbage, or a fake tmux)
        # must never reach disclosure, let alone the kill.
        run, log = _poisoned_slot(door, snap="930000|7|0|4242|bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED"
        try:
            proc = run("genesis-3-1")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert "runs NO claude" not in proc.stderr
        assert not run.killlog.exists() or run.killlog.read_text() == ""

    def test_session_id_validation_is_anchored(self, door):
        """A trailing-glob id check accepted `$7` followed by ANY text.

        Not exploitable (the value is quoted into `kill-session -t`), but this
        block's contract is that a value it cannot fully account for never
        reaches the kill. Uses a shell-metacharacter-bearing id assembled here
        so the assertion is about the ANCHOR, not about quoting.
        """
        run, log = _poisoned_slot(door, snap="930000|$7 ; echo pwned|0|4242|bash\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text() == "", (
            f"a malformed session id reached the kill:\n{run.killlog.read_text()}"
        )
        assert "runs NO claude" not in out, (
            f"a malformed id should fall through to attach before disclosure:\n{out}"
        )
        assert "new-session" in log.read_text()

    def test_f2_falsifier_no_forbidden_mechanisms(self):
        """F2, pre-registered with the user: the rebuild uses NO send-keys,
        NO respawn-pane, NO set-environment — those are the imitation-class
        mechanisms whose create/heal drift generated four review rounds — and
        exactly ONE new-session invocation exists (the create path IS the
        heal path)."""
        text = _CC_SLOT.read_text()
        assert "send-keys" not in text
        assert "respawn-pane" not in text
        assert "set-environment" not in text
        invocations = [
            ln for ln in text.splitlines()
            if "new-session" in ln and not ln.lstrip().startswith("#")
        ]
        assert len(invocations) == 1, invocations
