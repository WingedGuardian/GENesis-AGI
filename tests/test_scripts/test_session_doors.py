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
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO_ROOT / "scripts" / "cc-slot.sh"
_BOOTSTRAP = _REPO_ROOT / "scripts" / "bootstrap.sh"
_BASH = shutil.which("bash") or "bash"

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
    # TOCTOU simulation: a session that is ABSENT when manual mode's selection
    # loop asks, and PRESENT when the rebuild branch asks later. That is the race
    # — a concurrent manual launch creating the slot in between — and it cannot
    # be reproduced with a stateless session list, because the same file answers
    # both questions.
    if [[ -n "${FAKE_TMUX_SESSIONS_APPEAR:-}" && "$name" == "$FAKE_TMUX_SESSIONS_APPEAR" ]]; then
        _n=0; [[ -f "$FAKE_TMUX_APPEAR_N" ]] && _n=$(cat "$FAKE_TMUX_APPEAR_N")
        echo $(( _n + 1 )) > "$FAKE_TMUX_APPEAR_N"
        [[ "$_n" -ge 1 ]] && exit 0
        exit 1
    fi
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
        # A TRUNCATED read: rows already on stdout, then the command dies (a
        # timeout, or the server going away mid-listing). The real tmux does
        # exactly this, and the partial rows are indistinguishable from a whole
        # session unless the EXIT STATUS is consulted.
        if [[ -n "${FAKE_TMUX_SNAP_PARTIAL_FAIL:-}" ]]; then exit 1; fi
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


# Fake venv python: cc-slot resolves GENESIS_ROOT from its entrypoint and
# consults a venv python for the slot-map liveness verdict. The fixture places
# that checkout outside HOME so a HOME-based root cannot satisfy these tests.
# The probe's own logic is unit-tested in tests/test_cc/test_slot_liveness.py.
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
if [[ "$*" == *session_cap* ]]; then
  # The rebuild's pre-kill admission probe. Speaks session_cap's 3-line stdout
  # protocol: action / message / machine reason. FAKE_CAP_REASON empty means
  # "probe unavailable" (exit 1, no output) — the door's documented fail-open.
  [[ -n "${FAKE_CAP_LOG:-}" ]] && echo "$*" >> "$FAKE_CAP_LOG"
  if [[ -z "${FAKE_CAP_REASON:-}" ]]; then exit 1; fi
  case "$FAKE_CAP_REASON" in
    oom_floor)
      echo "RECLAIM"; echo "RAM low (512MB free, need >= 3072MB to start safely)." ;;
    cap_reached)
      # The population already exceeds the cap (the operator lowered it, or an
      # older build seeded more slots), so even a net-zero rebuild is refused.
      echo "RECLAIM"; echo "Session cap reached (4/3) — end one to continue." ;;
    cap_full)
      echo "DENY"; echo "Session cap reached (4/3)." ;;
    *)
      echo "ALLOW"; echo "Slot available." ;;
  esac
  echo "$FAKE_CAP_REASON"
  exit 0
fi
exit 1
"""


# Fake claude: the subcommand bypass `exec`s the real binary instead of building
# a tmux session, so the only way to prove the bypass ran — rather than merely
# that tmux was not called — is to see claude receive the argv. Logs and exits 0.
_FAKE_CLAUDE = """#!/usr/bin/env bash
[[ -n "${FAKE_CLAUDE_LOG:-}" ]] && printf '%s\\n' "$*" >> "$FAKE_CLAUDE_LOG"
# The slot path pins cwd to $GENESIS_ROOT before running claude; the bypass must
# match it, because mcp scope, project state and doctor's settings read are all
# cwd-keyed. Recording pwd is the only way to assert that from outside.
[[ -n "${FAKE_CLAUDE_CWD:-}" ]] && pwd > "$FAKE_CLAUDE_CWD"
# Distinguish UNSET from empty: an exported-but-empty TMPDIR is a different
# (worse) bug than no TMPDIR, and `${TMPDIR:-}` would conflate them.
[[ -n "${FAKE_CLAUDE_ENV:-}" ]] && printf 'TMPDIR=[%s]\n' "${TMPDIR-<unset>}" > "$FAKE_CLAUDE_ENV"
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

    fake_claude = bin_dir / "claude"
    fake_claude.write_text(_FAKE_CLAUDE)
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)

    home = tmp_path / "home"
    home.mkdir()
    checkout = tmp_path / "checkout"
    scripts_dir = checkout / "scripts"
    venv_bin = checkout / ".venv" / "bin"
    scripts_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    script_path = scripts_dir / "cc-slot.sh"
    shutil.copy2(_CC_SLOT, script_path)
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
    cap_log = tmp_path / "cap_calls.txt"
    claude_log = tmp_path / "claude.log"
    claude_cwd = tmp_path / "claude_cwd.txt"
    claude_env = tmp_path / "claude_env.txt"

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
            "FAKE_CLAUDE_LOG": str(claude_log),
            "FAKE_CLAUDE_CWD": str(claude_cwd),
            "FAKE_CLAUDE_ENV": str(claude_env),
            "FAKE_TMUX_KILLLOG": str(killlog),
            "FAKE_TMUX_KILL_RC": os.environ.get("_TEST_FAKE_KILL_RC", "0"),
            # Pre-kill admission probe: "" = unavailable (fail-open),
            # "oom_floor" = refuse the rebuild, anything else = admit.
            "FAKE_CAP_REASON": os.environ.get("_TEST_FAKE_CAP_REASON", ""),
            "FAKE_CAP_LOG": str(cap_log),
            # A slot that appears between manual mode's selection and the
            # rebuild branch's check — the concurrent-launch race.
            "FAKE_TMUX_SNAP_PARTIAL_FAIL": os.environ.get(
                "_TEST_FAKE_SNAP_PARTIAL_FAIL", ""
            ),
            "FAKE_TMUX_SESSIONS_APPEAR": os.environ.get(
                "_TEST_FAKE_SESSION_APPEARS", ""),
            "FAKE_TMUX_APPEAR_N": str(tmp_path / "appear_calls.txt"),
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
            ["bash", str(run.script_path), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    run.env_fn = _env  # so the pty runner builds the IDENTICAL environment
    run.script_path = script_path
    run.probe_log = probe_log  # ordered log of which slot-map probes ran
    run.snap = snap  # consent-block snapshot 1 (5-field lines)
    run.snap2 = snap2  # snapshot served from the SECOND call on (TOCTOU tests)
    run.killlog = killlog  # exact kill-session targets, one per line
    run.cap_log = cap_log  # argv of each pre-kill admission probe
    run.claude_log = claude_log  # argv of a claude the door exec'd DIRECTLY
    run.claude_cwd = claude_cwd  # cwd that claude inherited from the bypass
    run.claude_env = claude_env  # TMPDIR claude inherited (unset vs empty)
    run.home = home  # so a test can make the temp-dir candidates unusable
    run.checkout = checkout
    return run, log, sessions, listing, panes


def _new_session_line(log: Path) -> str:
    lines = [ln for ln in log.read_text().splitlines() if "new-session" in ln]
    assert len(lines) == 1, f"expected exactly one new-session call, got: {lines}"
    return lines[0]


class TestManualMode:
    def test_arbitrary_checkout_symlink_runs_probe_and_bypass_in_checkout(
        self, door, tmp_path
    ):
        """The door must follow its entrypoint to the checkout it belongs to.

        The fake venv deliberately lives outside HOME/genesis, and the script is
        invoked through a symlink, so a HOME-based root silently skips the real
        liveness probe and leaves the bypass in the caller's cwd.
        """
        run, _log, sessions, listing, panes = door
        checkout = run.checkout
        script_path = checkout / "scripts" / "cc-slot.sh"

        link = tmp_path / "slot-door"
        try:
            link.symlink_to(script_path)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip(
                    "Windows symlink creation requires developer mode or "
                    "SeCreateSymbolicLinkPrivilege"
                )
            raise
        run.script_path = link

        sessions.write_text("cc-1\n")
        listing.write_text("cc-1|0|Thu Jul 16 20:00:00 2026\n")
        panes.write_text("4242\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "ALIVE"
        try:
            proc = run("manual")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert proc.returncode == 0, proc.stderr
        assert run.probe_log.read_text().split() == ["liveness"], proc.stderr

        proc = run("manual", "mcp", "list")
        assert proc.returncode == 0, proc.stderr
        assert run.claude_cwd.read_text().strip() == str(checkout)

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

    def test_wrapper_renders_the_bootstrap_checkout_path(self, block):
        assert "local arg genesis_root=__GENESIS_ROOT__" in block
        assert "$HOME/genesis/scripts/cc-slot.sh" not in block
        text = _BOOTSTRAP.read_text()
        assert "printf '%q' \"$GENESIS_ROOT\"" in text
        assert 'TMUX_WRAP_BLOCK="${TMUX_WRAP_BLOCK//__GENESIS_ROOT__/' in text

    def test_wrapper_root_substitution_preserves_ampersands_and_shopt_state(self):
        text = _BOOTSTRAP.read_text()
        start = text.index('_tmux_wrap_root=$(printf')
        end = text.index('unset _tmux_wrap_root _tmux_wrap_patsub_replacement_was_set', start)
        substitution = text[start:end]
        checkout = "/tmp/genesis&slot"
        script = f"""\
set -euo pipefail
shopt -s patsub_replacement
GENESIS_ROOT={checkout!r}
TMUX_WRAP_BLOCK='root=__GENESIS_ROOT__'
{substitution}
printf '%s\\n' \"$TMUX_WRAP_BLOCK\"
shopt -q patsub_replacement
"""
        result = subprocess.run(
            [_BASH, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert result.stdout == "root=/tmp/genesis\\&slot\n"

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


def _run_door_pty(run, mode: str, feed: bytes, before_answer=None):
    """Run the door under a pty so the consent `read < /dev/tty` is reachable,
    with the EXACT environment the subprocess runner builds.

    `before_answer` runs once the consent PROMPT has actually been observed on
    the pty, and only then is the answer written. That lets a test change the
    world while the 120-second prompt waits — which is the entire reason the
    prompt is followed by a re-read — and it does so by WATCHING FOR THE PROMPT
    rather than sleeping, so it neither races on a slow box nor needs a thread
    (forkpty in a multi-threaded process is a documented deadlock risk).
    """
    import contextlib
    import pty
    import select
    import signal

    env = run.env_fn()
    pid, fd = pty.fork()
    if pid == 0:  # child: pty session leader
        try:
            os.execve("/bin/bash", ["bash", str(run.script_path), mode], env)  # noqa: S606
        except Exception:  # noqa: BLE001
            os._exit(127)
    out = b""
    timed_out = False
    if before_answer is not None:
        # Wait for the prompt itself; the door must have finished its FIRST
        # read of cc-slot.env before the callback edits it, or the test would
        # be asserting about a file the door had not yet loaded.
        deadline = time.monotonic() + 8
        while b"[y/N]" not in out and time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        assert b"[y/N]" in out, f"consent prompt never appeared:\n{out!r}"
        before_answer()
    os.write(fd, feed)
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 8)
        except OSError:
            break
        if not r:
            timed_out = True
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    if timed_out:
        # The read loop is bounded by select; os.waitpid is NOT. Leaving the loop
        # on a timeout means the door produced no further output — typically it
        # is parked at the consent prompt, whose own `read -t 120` would hold the
        # test for two minutes, and a child blocked for any other reason would
        # hold it forever. There is no subprocess.run(timeout=...) equivalent on
        # the pty path, so the deadline has to be enforced here.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
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


class TestRebuildAdmitsBeforeDestroying:
    """The one precondition read that belongs BEFORE the destructive action.

    Every other read in this script is deliberately post-kill so nothing can go
    stale — but the capacity gate downstream can REFUSE after the slot is gone.
    `_cap_reclaim` has six `exit 1` paths and all of them run post-kill; the
    likeliest is mundane: consent, kill, gate returns RECLAIM, operator presses
    Enter to cancel. Pane and scrollback destroyed, no replacement.

    Only the RAM floor can legitimately refuse a REBUILD. The COUNT check cannot:
    the slot is counted before and counted again after, so a rebuild is net-zero
    (reattach already bypasses the cap for the same reason). So the pre-kill probe
    asks the SHIPPED decision engine about the post-kill world and acts only on a
    verdict of `oom_floor`.
    """

    def test_low_ram_refuses_the_rebuild_and_keeps_the_slot(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "oom_floor"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        # THE POINT: consent was given, and the slot still exists.
        assert not run.killlog.exists() or run.killlog.read_text().strip() == "", (
            f"the slot was destroyed even though the replacement could not start:\n{out}"
        )
        assert "NOT rebuilding" in out, out
        # The operator is told WHY and what to do — a refusal they cannot act on
        # is just a different way to lose.
        assert "RAM is below the floor" in out, out
        assert "free memory" in out, out
        # And it still attaches rather than dropping them at a bare prompt.
        assert "new-session" in log.read_text()

    def test_healthy_ram_still_rebuilds(self, door):
        """The control. A refusal that fires always would 'pass' the test above
        while removing the feature."""
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        assert run.killlog.read_text().strip() == "$7", (
            f"a healthy rebuild must still kill by id:\n{out}"
        )

    def test_probe_models_the_post_kill_world(self, door):
        """`--existing` must be the count AFTER this slot goes, or the COUNT
        check could refuse a rebuild that is net-zero by construction."""
        run, _ = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        argv = run.cap_log.read_text()
        assert "--existing" in argv, argv
        # The fixture's listing holds exactly one cc-N session, so post-kill is 0.
        assert "--existing 0" in argv, (
            f"probe must model the post-kill count, not the current one: {argv}"
        )

    def test_unavailable_probe_fails_open(self, door):
        """Stated rather than hidden: if the probe cannot run, the door behaves
        exactly as it did before this change. This NARROWS the window, it does
        not close it, and a test that pretended otherwise would be a lie."""
        run, _ = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        # FAKE_CAP_REASON unset -> the fake exits 1 with no output.
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
        assert code == 0, out
        assert run.killlog.read_text().strip() == "$7", out


class TestCountBasedRefusalsAlsoKeepTheSlot:
    """Only ALLOW may proceed — the RAM floor is not the only refusal.

    The first version of this admission check refused on `oom_floor` alone,
    reasoning that a rebuild is net-zero so the COUNT gate cannot object. That
    was right about the DELTA and wrong about the ABSOLUTE: when the population
    ALREADY exceeds the cap (the operator lowered it, an older build seeded more
    slots), post-kill `existing` is still over and the gate answers DENY or
    RECLAIM. RECLAIM is not a safe yes either — declining its prompt exits 1
    with the pane already destroyed.
    """

    @pytest.mark.parametrize("reason", ["cap_reached", "cap_full"])
    def test_a_count_refusal_keeps_the_pane(self, door, reason):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = reason
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        assert not run.killlog.exists() or run.killlog.read_text().strip() == "", (
            f"the slot was destroyed for a replacement the cap would refuse:\n{out}"
        )
        assert "NOT rebuilding" in out, out
        # Named, so the operator can act — a refusal they cannot act on is just
        # a different way to lose.
        assert reason in out, out
        assert "new-session" in log.read_text()


class TestDeletedLeverRevertsOnReRead:
    """Sourcing only OVERLAYS, so a deleted assignment used to survive.

    The 120-second consent prompt is exactly when an operator goes and edits
    this file — the message they just read is what sent them there. If they
    REMOVE `GENESIS_CC_PERMISSION_MODE=bypass`, a bare re-source leaves the old
    value in the shell and the rebuilt pane still launches
    `--dangerously-skip-permissions`. The loader now clears the known levers
    and restores the pre-source environment before reading the file.
    """

    def test_removing_the_permission_lever_disarms_the_rebuild(self, door):
        run, log = _poisoned_slot(door)
        env_file = run.home / ".genesis" / "cc-slot.env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("GENESIS_CC_PERMISSION_MODE=bypass\n")

        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            # The operator goes and edits the file while the prompt waits —
            # which is exactly what the message they just read sends them to do.
            code, out = _run_door_pty(
                run, "genesis-3-1", b"y\n",
                before_answer=lambda: env_file.write_text("# lever removed\n"),
            )
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        launched = log.read_text()
        assert "new-session" in launched
        assert "--dangerously-skip-permissions" not in launched, (
            "the rebuild used a permission mode the operator had already removed:"
            f"\n{launched}"
        )

    def test_the_lever_still_applies_while_it_is_present(self, door):
        """The control. A loader that dropped the lever unconditionally would
        'pass' the test above while removing the feature."""
        run, log = _poisoned_slot(door)
        env_file = run.home / ".genesis" / "cc-slot.env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("GENESIS_CC_PERMISSION_MODE=bypass\n")
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        assert "--dangerously-skip-permissions" in log.read_text(), out


class TestEveryBoundedGateHasAKillDeadline:
    """`timeout N` sends TERM and then WAITS; `-k` is what adds the KILL.

    Two of these gates run AFTER the consent rebuild has already destroyed the
    slot, so a probe that ignores TERM leaves the operator with neither the old
    pane nor a replacement. Asserted over the whole script rather than the two
    known sites, so a bounded call added next year inherits the rule.
    """

    def test_no_timeout_without_kill_after(self):
        offenders = []
        for i, line in enumerate(_CC_SLOT.read_text().split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "timeout " not in stripped:
                continue
            for frag in stripped.split("timeout ")[1:]:
                if not frag.startswith("-k "):
                    offenders.append(f"{i}: {stripped}")
                    break
        assert not offenders, (
            "a TERM-only bound is not a deadline against a process wedged in "
            f"uninterruptible I/O: {offenders}"
        )


class TestTruncatedSnapshotNeverAuthorizesAKill:
    """A partial `list-panes` read must not pass as a snapshot.

    `tmux list-panes` can write rows and THEN die — a timeout, or the server
    going away mid-listing. The rows already on stdout are indistinguishable
    from a complete session unless the exit status is consulted, and the door
    used to swallow that status with `|| true`. In a multi-pane session the
    omitted row can be the one running claude: both reads then see the same
    bash-only subset, both probe POISONED, the projections compare EQUAL, every
    identity check passes, and a live session is destroyed by a door whose
    entire contract is "anything unrecognised attaches".

    The status is now taken separately and a nonzero exit yields an EMPTY
    snapshot, which is already this block's "no verdict".
    """

    def test_a_partial_read_falls_toward_attach(self, door):
        run, log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_SNAP_PARTIAL_FAIL"] = "1"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_SNAP_PARTIAL_FAIL", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        # THE POINT: nothing was killed on the strength of a truncated read.
        assert not run.killlog.exists() or run.killlog.read_text().strip() == "", (
            f"a truncated snapshot authorized a kill:\n{out}"
        )
        # And the operator is not stranded — the door still attaches.
        assert "new-session" in log.read_text()

    def test_a_clean_read_of_the_same_rows_still_rebuilds(self, door):
        """The control, and it is load-bearing.

        Identical fixture, identical rows, identical liveness verdicts — the
        ONLY difference is tmux's exit status. Without this the test above
        passes against a door that never rebuilds anything.
        """
        run, _ = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        assert run.killlog.read_text().strip() == "$7", out


class TestNothingSitsBetweenTheLastReadAndTheKill:
    """The admission probe runs BEFORE the final snapshot, not after it.

    The probe is a Python start-up behind a 15s+2s bound. Sequenced after the
    snapshot/liveness compare — which is where it first landed — it re-opened
    the exact staleness window this block exists to retire: the operator
    consents to a disclosed state, the probe spends seconds, someone starts a
    process in that pane, and the kill lands on a session that no longer
    matches what was disclosed. There is no third snapshot to catch it.

    Ordering is the whole fix, so it is pinned structurally: the last read
    before `kill-session` must be the snapshot compare.
    """

    def test_the_capacity_probe_precedes_the_final_snapshot(self):
        text = _CC_SLOT.read_text()
        code = [
            ln for ln in text.split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        probe = next(
            (i for i, ln in enumerate(code) if "session_cap --existing" in ln), None
        )
        snap2 = next(
            (i for i, ln in enumerate(code) if "_s2_snap2=$(_s2_snapshot)" in ln), None
        )
        kill = next(
            (i for i, ln in enumerate(code) if 'kill-session -t "$_s2_sid1"' in ln), None
        )
        assert probe is not None, "the rebuild path must admit before destroying"
        assert snap2 is not None, "the rebuild path must re-read state after consent"
        assert kill is not None, "the rebuild path must kill by id"
        assert probe < snap2, (
            "the capacity probe must run BEFORE the final snapshot — after it, "
            "its own latency becomes a stale window on the disclosed state"
        )
        assert snap2 < kill, (
            "the snapshot compare must be the LAST read before the kill"
        )

    def test_no_subprocess_bound_sits_between_the_compare_and_the_kill(self):
        """Guard the class, not just today's instance.

        The ordering test above would still pass if a NEW bounded call were
        added between the compare and the kill. Name the window itself: no
        `timeout` (the shape every probe here takes) may appear in it.
        """
        text = _CC_SLOT.read_text()
        start = text.index("_s2_verdict2=$(_s2_liveness")
        end = text.index('kill-session -t "$_s2_sid1"', start)
        window = [
            ln for ln in text[start:end].split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        offenders = [ln for ln in window if "timeout " in ln]
        assert not offenders, (
            "a bounded subprocess between the final state read and the kill is "
            f"a stale window on the operator's consent: {offenders}"
        )


class TestRebuildIsHostnameModeOnly:
    """Manual mode allocated a slot with NO live session, so reaching the rebuild
    branch at all means something created it in between — a concurrent manual
    launch. Relying on that earlier availability probe is a TOCTOU whose window
    spans the cc-slot.env sourcing, and whose consequence is the worst one: a
    session that is still starting has no claude child yet, probes POISONED, and
    an affirmative answer kills the session someone just created.
    """

    def test_manual_mode_never_offers_to_rebuild_under_the_race(self, door):
        """Reproduces the actual TOCTOU, not merely 'manual mode picks a free slot'.

        A first draft of this test only arranged an existing cc-1, so manual mode
        selected cc-2, the rebuild branch was never reached, and the test PASSED
        with the guard deleted — vacuous for its stated purpose, and caught by
        mutating the guard rather than by reading it.

        The real shape needs a session that is ABSENT when the selection loop asks
        and PRESENT when the rebuild branch asks, which is what a concurrent manual
        launch does. `FAKE_TMUX_SESSIONS_APPEAR` produces exactly that.
        """
        run, log = _poisoned_slot(door)
        # cc-1 exists, so selection moves to cc-2 — which then "appears".
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_SESSION_APPEARS"] = "cc-2"
        try:
            code, out = _run_door_pty(run, "manual", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_SESSION_APPEARS", None)
        assert code == 0, out
        assert "runs NO claude" not in out, (
            f"manual mode entered the rebuild branch on a slot a concurrent "
            f"launch had just created:\n{out}"
        )
        assert not run.killlog.exists() or run.killlog.read_text().strip() == "", (
            f"manual mode killed a session someone else had just created:\n{out}"
        )

    def test_hostname_mode_still_rebuilds(self, door):
        """The control: the gate must exclude manual mode WITHOUT disabling the
        feature on the door it exists for."""
        run, _log = _poisoned_slot(door)
        os.environ["_TEST_FAKE_LIVENESS"] = "POISONED,POISONED"
        os.environ["_TEST_FAKE_CAP_REASON"] = "ok"
        try:
            code, out = _run_door_pty(run, "genesis-3-1", b"y\n")
        finally:
            os.environ.pop("_TEST_FAKE_LIVENESS", None)
            os.environ.pop("_TEST_FAKE_CAP_REASON", None)
        assert code == 0, out
        assert run.killlog.read_text().strip() == "$7", out


class TestAdmissionSurvivesTheKill:
    """A sampled precondition that does not survive the action it guards is not
    a guard.

    The preflight says the rebuild is admissible, the slot is destroyed, and
    then the ORDINARY capacity gate runs again on the way to the replacement —
    a second, differently-timed, fallible answer that can be DENY or RECLAIM
    with the pane already gone. `_cap_reclaim` alone has six `exit 1` paths.
    Narrowing that window was the wrong shape; the admitted rebuild now bypasses
    the second gate entirely, exactly as a REATTACH does, and for the identical
    net-zero reason the script already states for reattach.
    """

    def test_the_rebuild_carries_its_admission_to_the_gate(self):
        code = [
            ln for ln in _CC_SLOT.read_text().split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        set_i = next((i for i, ln in enumerate(code) if "_CC_REBUILT=1" in ln), None)
        gate_i = next(
            (i for i, ln in enumerate(code) if '"$_CC_REBUILT" = "1"' in ln), None
        )
        cap_i = next(
            (i for i, ln in enumerate(code) if "_cap_out=$(timeout" in ln), None
        )
        assert set_i is not None, "a successful rebuild must record its admission"
        assert gate_i is not None, "the capacity gate must honour a carried admission"
        assert cap_i is not None
        assert set_i < gate_i < cap_i, (
            "the carried admission must be consulted BEFORE the ordinary gate "
            "runs, or the second answer can still take the replacement away"
        )

    def test_the_flag_is_initialized_before_the_consent_block(self):
        """`set -u` is on; an unset flag would abort the login outright, and on
        the path where no rebuild happens it is never assigned."""
        code = [
            ln for ln in _CC_SLOT.read_text().split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        init = next((i for i, ln in enumerate(code) if ln.strip() == "_CC_REBUILT=0"), None)
        use = next((i for i, ln in enumerate(code) if "_CC_REBUILT=1" in ln), None)
        assert init is not None and use is not None and init < use

    def test_the_oauth_gate_is_not_bypassed_by_a_rebuild(self):
        """Deliberately unlike `_SESSION_EXISTS`. A reattach skips the OAuth
        probe because attaching does NOT re-run the pane command, so the token
        work would be moot. A rebuild DOES re-run it, so that reasoning does not
        transfer and the probe must still happen."""
        text = _CC_SLOT.read_text()
        oauth = text.index("_oauth_notice=$(timeout")
        # The bypass branch must not extend over the OAuth gate.
        branch = text.index('"$_CC_REBUILT" = "1"')
        assert branch < oauth
        window = text[branch:oauth]
        assert "_oauth_notice" not in window


class TestSubcommandBypass:
    """An EPHEMERAL `claude <subcommand>` must not be given a persistent slot.

    It prints and exits, so a slot occupies capacity the operator wanted for a
    session, leaves a tmux session behind, and captures the pane's scrollback on
    exit — which for `setup-token` is a long-lived credential. The capture is
    already scrubbed and fails closed, so this is defence in depth.

    The split is by LIFETIME, not by "is it listed under Commands:". Three of
    claude's subcommands are exactly what the slot exists for, and bypassing
    them would hand an SSH drop the failure this launcher prevents.
    """

    # Ephemeral: prints and exits. These bypass.
    EPHEMERAL = [
        "auth", "auto-mode", "doctor", "import", "install", "mcp", "plugin",
        "plugins", "project", "setup-token", "update", "upgrade",
    ]
    # Long-lived: a daemon, the interactive agent view, and a minutes-long
    # cloud review. These KEEP the slot, as a recorded decision.
    KEEPS_A_SLOT = ["agents", "gateway", "ultrareview"]

    @staticmethod
    def _door_list(name: str) -> set[str]:
        """Parse a bash array literal out of cc-slot.sh."""
        text = _CC_SLOT.read_text()
        line = next(
            (ln for ln in text.splitlines() if ln.startswith(f"{name}=(")), None
        )
        assert line is not None, (
            f"{name} not found in cc-slot.sh — it was renamed or indented, and "
            "the tests below would then be checking nothing"
        )
        return set(line.split("(", 1)[1].rstrip(") \t").split())

    # ---- the bypass -------------------------------------------------------

    def test_setup_token_is_handed_straight_to_claude(self, door):
        """The acceptance bar: the case the bypass exists for.

        Asserts what RAN, not merely that tmux was idle — a door that crashed
        before reaching tmux would also leave the log empty.
        """
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "setup-token")
        assert result.returncode == 0, result.stderr
        assert run.claude_log.exists(), "claude was never exec'd"
        assert run.claude_log.read_text().strip() == "setup-token"
        assert not log.exists() or "new-session" not in log.read_text(), (
            "setup-token was given a tmux slot, so its output would be captured"
        )

    @pytest.mark.parametrize("sub", EPHEMERAL)
    def test_every_ephemeral_subcommand_bypasses_the_slot(self, door, sub):
        run, log, _sessions, _listing, _panes = door
        result = run("manual", sub)
        assert result.returncode == 0, result.stderr
        assert run.claude_log.read_text().strip() == sub
        assert not log.exists() or "new-session" not in log.read_text()

    def test_arguments_after_the_subcommand_are_passed_through(self, door):
        """`claude mcp list` must reach claude intact, not as a bare `mcp`."""
        run, _log, _sessions, _listing, _panes = door
        result = run("manual", "mcp", "list", "--scope", "user")
        assert result.returncode == 0, result.stderr
        assert run.claude_log.read_text().strip() == "mcp list --scope user"

    def test_the_bypass_runs_in_the_repo_like_the_slot_path_does(self, door):
        """cwd must match the slot path's `cd ${GENESIS_ROOT} && claude`.

        `mcp` scope, `project` state and `doctor`'s settings read are all keyed
        on the working directory, and an SSH login shell starts in $HOME. A
        bypass that inherited the caller's cwd would silently retarget them —
        `mcp list` would not see the repo's own .mcp.json, and `project purge`
        would name a different project.
        """
        run, _log, _sessions, _listing, _panes = door
        result = run("manual", "mcp", "list")
        assert result.returncode == 0, result.stderr
        assert run.claude_cwd.exists(), "claude never recorded a cwd"
        assert run.claude_cwd.read_text().strip() == str(run.checkout)

    # ---- what must STILL get a slot ---------------------------------------

    @pytest.mark.parametrize("sub", KEEPS_A_SLOT)
    def test_long_lived_subcommands_keep_their_slot(self, door, sub):
        """A daemon / TUI / minutes-long job must survive a dropped SSH.

        Bypassing these would tie them to the connection, which is the exact
        failure the slot exists to prevent.
        """
        run, log, _sessions, _listing, _panes = door
        result = run("manual", sub)
        assert result.returncode == 0, result.stderr
        # Check existence first: without it a bypassed `sub` never calls tmux,
        # and this fails with a bare FileNotFoundError instead of saying why.
        assert log.exists(), f"{sub} never reached tmux — it was bypassed"
        assert "new-session" in log.read_text(), f"{sub} lost its slot"
        assert not run.claude_log.exists(), f"{sub} was exec'd directly"

    def test_a_bare_interactive_session_still_gets_a_slot(self, door):
        run, log, _sessions, _listing, _panes = door
        result = run("manual")
        assert result.returncode == 0, result.stderr
        assert "new-session" in log.read_text()
        assert not run.claude_log.exists(), "the door exec'd claude directly"

    def test_a_prompt_naming_a_subcommand_still_gets_a_slot(self, door):
        """`claude "mcp is broken"` is a PROMPT, not the `mcp` subcommand."""
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "mcp is broken")
        assert result.returncode == 0, result.stderr
        assert "new-session" in log.read_text()
        assert not run.claude_log.exists()

    def test_a_flag_whose_value_names_a_subcommand_still_gets_a_slot(self, door):
        """Only $1 is matched, so a flag value is never mistaken for one."""
        run, log, _sessions, _listing, _panes = door
        result = run("manual", "--agent", "mcp")
        assert result.returncode == 0, result.stderr
        assert "new-session" in log.read_text()
        assert not run.claude_log.exists()

    def test_hostname_mode_never_bypasses(self, door):
        """The bypass is gated on manual mode; a slot door keeps its slot."""
        run, log, _sessions, _listing, _panes = door
        result = run("genesis-3-4", "setup-token")
        assert result.returncode == 0, result.stderr
        assert log.exists(), "hostname mode was bypassed — tmux never ran"
        assert "-s cc-4" in log.read_text()
        assert not run.claude_log.exists()

    # ---- drift ------------------------------------------------------------

    def test_the_door_and_the_test_declare_the_same_lists(self):
        """Guard-the-guard: both lists must be the ones the door enforces."""
        assert self._door_list("_CC_EPHEMERAL") == set(self.EPHEMERAL)
        assert self._door_list("_CC_KEEPS_A_SLOT") == set(self.KEEPS_A_SLOT)

    def test_the_two_lists_are_disjoint(self):
        assert not set(self.EPHEMERAL) & set(self.KEEPS_A_SLOT)

    def test_every_installed_subcommand_is_CLASSIFIED(self):
        """A new subcommand must be CLASSIFIED, not defaulted into the bypass.

        Polarity matters here. Asserting "everything installed is in the bypass
        list" would make the only green-again move for a future `claude serve`
        be to bypass it — turning a curated allowlist into an auto-expanding
        denylist, which is how a daemon would end up losing its slot. So the
        test demands a DECISION into one list or the other.

        Skipped where claude is absent (a fresh clone, a CI runner), which makes
        this a LOCAL drift detector rather than a gate. Said plainly, because a
        test that silently skips everywhere reads like coverage it is not.
        """
        import shutil

        if shutil.which("claude") is None:
            pytest.skip("claude is not installed — cannot check list drift")
        helptext = subprocess.run(
            ["claude", "--help"], capture_output=True, text=True, timeout=60
        ).stdout
        _, _, commands = helptext.partition("Commands:")
        assert commands.strip(), "could not find the Commands: section in --help"
        installed = {
            line.split()[0].split("|")[0]
            for line in commands.splitlines()
            if line.startswith("  ") and line.strip() and not line.startswith("    ")
        }
        assert installed, "parsed zero subcommands — the --help layout changed"
        classified = set(self.EPHEMERAL) | set(self.KEEPS_A_SLOT)
        unclassified = installed - classified - {"help"}
        assert not unclassified, (
            "claude ships subcommands this door has not classified. Decide per "
            "entry: prints-and-exits -> _CC_EPHEMERAL; daemon / TUI / "
            f"long-running -> _CC_KEEPS_A_SLOT. Unclassified: {sorted(unclassified)}"
        )


class TestBypassTempDirIsValidatedNotAssumed:
    """The bypass must apply the SAME temp-dir validation as the slot path.

    It used to test `-d "$HOME/tmp"` and export it. `-d` says a directory
    exists, not that it is ours, writable, or private — so a root-owned
    leftover from an earlier `sudo` run was accepted, and `claude install` /
    `claude update` would unpack state into a directory this script never
    established anyone else could not read. The slot path a few hundred lines
    down creates the candidate, checks writability, and requires `chmod 700`.

    Both now call one `_cc_resolve_tmpdir`, so there is a single policy rather
    than two that can drift — which is what let the weaker copy exist at all.
    """

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bit")
    def test_an_unusable_candidate_is_not_exported_to_a_bypassed_subcommand(
        self, door
    ):
        """Unusable candidates must leave TMPDIR unset, never pointed at them."""
        run, _log, _sessions, _listing, _panes = door
        home = Path(run.home)
        for cand in (home / ".genesis" / "cc-tmp", home / "tmp"):
            cand.mkdir(parents=True, exist_ok=True)
            # 0500 fails the WRITABILITY test, so `continue` fires before the
            # chmod is ever attempted. The chmod-rejection branch needs a
            # foreign-owned world-writable directory and is NOT covered here.
            cand.chmod(0o500)
        try:
            proc = run("manual", "update")
        finally:
            for cand in (home / ".genesis" / "cc-tmp", home / "tmp"):
                cand.chmod(0o700)  # so tmp_path teardown can clean up
        assert proc.returncode == 0, proc.stderr
        assert run.claude_env.exists(), "claude was never reached"
        observed = run.claude_env.read_text().strip()
        assert observed == "TMPDIR=[<unset>]", (
            "the bypass exported a temp dir it had not validated; the slot path "
            f"rejects this same directory. Got: {observed}"
        )

    def test_a_usable_candidate_IS_exported_to_a_bypassed_subcommand(self, door):
        """Negative control: without it the test above passes on a broken bypass.

        A bypass that simply never set TMPDIR would satisfy the unusable case
        while losing the reason the resolution exists — `install`/`update`
        unpacking into the small ambient temp.
        """
        run, _log, _sessions, _listing, _panes = door
        home = Path(run.home)
        (home / ".genesis" / "cc-tmp").mkdir(parents=True, exist_ok=True)
        proc = run("manual", "update")
        assert proc.returncode == 0, proc.stderr
        observed = run.claude_env.read_text().strip()
        assert observed == f"TMPDIR=[{home / '.genesis' / 'cc-tmp'}]", (
            f"a usable candidate was not exported to the bypass. Got: {observed}"
        )

    def test_an_explicit_caller_TMPDIR_is_left_alone(self, door):
        """An operator who exported TMPDIR chose it; do not second-guess them."""
        run, _log, _sessions, _listing, _panes = door
        home = Path(run.home)
        (home / ".genesis" / "cc-tmp").mkdir(parents=True, exist_ok=True)
        os.environ["_TEST_INHERITED_TMPDIR"] = "/inherited/from/parent"
        try:
            proc = run("manual", "update")
        finally:
            os.environ.pop("_TEST_INHERITED_TMPDIR", None)
        assert proc.returncode == 0, proc.stderr
        observed = run.claude_env.read_text().strip()
        assert observed == "TMPDIR=[/inherited/from/parent]", (
            f"the bypass overrode a TMPDIR the caller had set. Got: {observed}"
        )

    def test_one_resolution_function_serves_both_paths(self):
        """Guard-the-guard: the duplication must not quietly come back.

        The defect was two temp-dir policies, one weaker. If a future edit
        reintroduces a second candidate loop, the tests above still pass while
        the drift they exist to prevent is back.
        """
        body = _CC_SLOT.read_text()
        assert body.count("_cc_resolve_tmpdir()") == 1, "more than one definition"
        assert body.count('for _cand in "$HOME/.genesis/cc-tmp"') == 1, (
            "a second candidate loop exists outside _cc_resolve_tmpdir"
        )
        # NOT a `count(...) >= 3` check: there are four mentions and one is
        # prose, so deleting a real call site still satisfies it. Assert the
        # call sites themselves, which is what could actually regress.
        assert "|| _cc_resolve_tmpdir\n" in body or "! _cc_resolve_tmpdir" in body, (
            "the bypass no longer calls the shared resolution"
        )
        assert "if _cc_resolve_tmpdir; then" in body, (
            "the slot path no longer calls the shared resolution"
        )
