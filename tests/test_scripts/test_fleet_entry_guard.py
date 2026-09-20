"""Tests for scripts/fleet_entry_guard.sh — the fleet stale-pane-mode guard.

WHAT IS UNDER TEST, and why it is worth pinning
-----------------------------------------------
An operator intermittently lands in a "frozen session with a yellow line". The
yellow is tmux's ``mode-style``: the pane is in a MODE (``choose-tree`` or
``copy-mode``), not frozen. ``scripts/lobby-door.sh`` fixed that for the LOBBY
pane by giving every connection its own throwaway picker; it never reaches the
panes the picker SELECTS, and a ``cc-*`` slot left in a mode holds it
indefinitely, because a mode belongs to the PANE rather than to the client.

The guard clears such panes at entry with ``copy-mode -q`` and records what it
found. It MUTATES STATE ON THE LOGIN PATH, which is why the safety conditions
below are tested harder than the happy path.

A pane is touched only when all three hold: it is in a mode, it is a real
destination rather than one of the doors' own transient ``lobby-<pid>`` pickers
(which are in tree-mode BY DESIGN — flagging those would emit an anomaly on
every connection ever made, indistinguishable from flagging nothing), and NO
client is attached to it anywhere.

That last condition is subtler than it reads, and got this wrong once:
``list-panes -a`` emits one row per (SESSION, pane) and ``session_attached`` is
a SESSION property, so a shared window puts the same pane on two rows with
different attached counts. The test named for that is the one to keep.

Three layers here, deliberately:

* classification tests drive the real script through an injectable pane source,
  so they run on ANY runner including one with no tmux — a skip guard there
  would let CI prove nothing about the only logic in the file;
* stub-tmux tests cover what the seam cannot reach: a clear that FAILS, the
  budget running out mid-sweep, and the operator lever. The stub WITNESSES the
  clear rather than assuming it, because an earlier version did assume it and
  reported success against a guard that never issued one;
* end-to-end tests drive a REAL tmux server on an isolated socket, and are
  skipped only where tmux genuinely does not exist.

Every fixture here is synthetic. No live session names, pids or ttys from this
install appear in this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "fleet_entry_guard.sh"


def _pane(
    name: str,
    in_mode: int,
    mode: str = "",
    pid: int = 4242,
    attached: int = 0,
    pane_id: str = "%1",
) -> str:
    """One synthetic `list-panes` line in the exact format the script requests.

    Field order is the point. Every DECISION field leads — the in-mode flag,
    tmux's own picker/dest verdict, the pane id, and the attached count —
    because a session name is free text that may contain SPACES, and a parse
    that searched the whole line could be forged by a crafted name.

    `picker` is what `#{?#{m:lobby-*,…},picker,dest}` yields for a
    `lobby-<pid>` session, so this reproduces tmux's verdict rather than
    re-deriving the glob in Python.

    `attached` defaults to 0 because that is the interesting case: an
    unattached pane is the one the guard may clear.
    """
    kind = "picker" if name.startswith("lobby-") else "dest"
    return (
        f"in_mode={in_mode} kind={kind} pane={pane_id} attached={attached} "
        f"{name}:1.0 mode={mode} pid={pid} cmd=bash"
    )


def _run(tmp_path: Path, panes: list[str], label: str = "lobby"):
    """Run the guard with pane data injected, in an isolated HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("\n".join(panes) + "\n" if panes else "")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GENESIS_FLEET_GUARD_PANES_FILE"] = str(panes_file)
    env.pop("TMUX", None)

    proc = subprocess.run(
        ["bash", str(GUARD), label],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    body = logs[0].read_text() if logs else ""
    return proc, body, logs


def test_both_doors_actually_invoke_the_guard():
    """Wiring, not existence — the gap an adversarial review found by DELETING
    both call sites and watching 102 tests pass anyway.

    A guard nothing calls is a repair that never runs, and every
    classification test in this file would stay green. Both entries are asserted
    because they are separate exposures: the picker (`-lobby`) is what the
    operator reports, and the numeric slot door (`-<N>`) reaches the same
    hazard through `new-session -A`, which attaches to an existing pane without
    clearing its mode.

    A FIRST VERSION OF THIS TEST WAS VACUOUS and a mutation caught it: it
    grepped each door for the string "fleet_entry_guard.sh", which still
    appears in the explanatory comment and in the `[ -x ... ]` guard after the
    invocation itself is deleted. Existence of the name is not evidence of a
    call. This version strips comments and requires an actual EXECUTION line —
    the guard alone does not satisfy it, because `[ -x X ]` tests X without
    running it.
    """
    for door in ("lobby-door.sh", "cc-slot.sh"):
        text = (REPO_ROOT / "scripts" / door).read_text()
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        invocations = [
            ln
            for ln in code
            if "fleet_entry_guard.sh" in ln and not ln.lstrip().startswith("[") and "-x " not in ln
        ]
        assert invocations, (
            f"scripts/{door} has no line that EXECUTES the fleet entry guard "
            "(a comment or an [ -x ] guard mentioning it is not a call)"
        )


def test_the_doors_resolve_the_guard_without_assuming_a_fixed_repo_root():
    """Generalizability: both doors must work on a clone that is not ~/genesis.

    cc-slot.sh hardcodes GENESIS_ROOT="${HOME}/genesis" for its own purposes (a
    separate known defect, deliberately not fixed here) — so the risk is a
    future edit reaching for that variable because it is right there.
    """
    for door in ("lobby-door.sh", "cc-slot.sh"):
        text = (REPO_ROOT / "scripts" / door).read_text()
        line = next(ln for ln in text.splitlines() if "fleet_entry_guard.sh" in ln)
        assert "GENESIS_ROOT" not in line, (
            f"scripts/{door} resolves the guard from a hardcoded root"
        )


def test_script_is_executable_and_parses():
    assert GUARD.exists(), f"{GUARD} is missing"
    assert os.access(GUARD, os.X_OK), "guard script must be executable"
    subprocess.run(["bash", "-n", str(GUARD)], check=True, timeout=60)


def test_a_moded_slot_is_flagged_as_an_anomaly(tmp_path):
    """The defect this exists for: a cc-* slot holding tree-mode."""
    _, body, _ = _run(tmp_path, [_pane("cc-3", 1, "tree-mode")])
    assert "ANOMALY moded-destination cc-3:1.0" in body, body


def test_a_clean_fleet_emits_no_anomaly(tmp_path):
    """Negative control. Without this, a script that flags EVERYTHING passes."""
    _, body, _ = _run(tmp_path, [_pane("cc-1", 0), _pane("cc-2", 0), _pane("lobby", 0)])
    assert "ANOMALY" not in body, body
    # ... but it must still have recorded the inventory, or "no anomaly" would
    # be indistinguishable from "read nothing at all".
    assert "cc-1:1.0" in body, body
    assert "cc-2:1.0" in body, body


def test_the_doors_own_picker_is_not_an_anomaly(tmp_path):
    """`lobby-<pid>` is the door's per-connection picker and is moded BY DESIGN.

    lobby-door.sh hides these from its own chooser with the same distinction
    (``-f '#{!=:#{m:lobby-*,#{session_name}},1}'``). Flagging them would emit an
    anomaly on every connection ever made.
    """
    _, body, _ = _run(tmp_path, [_pane("lobby-12345", 1, "tree-mode")])
    assert "ANOMALY" not in body, body


def test_a_moded_slot_is_still_found_beside_a_moded_picker(tmp_path):
    """The discriminating case — the exclusion must not blind the detection.

    A real connection has its own picker in tree-mode at the same moment the
    stale slot is. A filter that bailed on the first moded pane, or excluded too
    broadly, would pass both single-pane tests above and fail here.
    """
    _, body, _ = _run(
        tmp_path,
        [
            _pane("lobby-99999", 1, "tree-mode"),
            _pane("cc-5", 1, "tree-mode"),
            _pane("cc-6", 0),
        ],
    )
    assert body.count("ANOMALY") == 1, body
    assert "ANOMALY moded-destination cc-5:1.0" in body, body


def test_the_persistent_lobby_session_is_in_scope(tmp_path):
    """Boundary: the glob is ``lobby-*``, so bare ``lobby`` must NOT be excluded.

    The persistent workspace session is named ``lobby`` with no suffix and IS
    selectable from the picker, so a mode on it strands an operator exactly like
    a mode on a slot. An exclusion written as a substring match on "lobby"
    instead of the ``lobby-*`` glob would silently lose this case.
    """
    _, body, _ = _run(tmp_path, [_pane("lobby", 1, "tree-mode")])
    assert "ANOMALY moded-destination lobby:1.0" in body, body


def test_copy_mode_counts_too_not_just_tree_mode(tmp_path):
    """The operator reports "yellow"; `mode-style` covers every mode.

    A mouse scroll puts a pane in copy-mode, which paints the same yellow and
    leaves the same `in_mode=1`. Keying the detection on the tree-mode STRING
    rather than on the flag would miss it.
    """
    _, body, _ = _run(tmp_path, [_pane("cc-4", 1, "copy-mode")])
    assert "ANOMALY moded-destination cc-4:1.0" in body, body


def test_a_session_name_containing_spaces_cannot_forge_an_anomaly(tmp_path):
    """Session names are free text and CAN contain spaces.

    An earlier version tested `case "$line" in *" in_mode=1 "*)`, searching the
    WHOLE line — so a session literally named `x in_mode=1 mode=tree-mode y`
    produced an ANOMALY while sitting at in_mode=0. Found in adversarial review.
    The fix leads with tmux-generated fields; this pins it.
    """
    hostile = "evil in_mode=1 mode=tree-mode pad"
    _, body, _ = _run(tmp_path, [_pane(hostile, 0)])
    assert "ANOMALY" not in body, body


def test_a_session_name_with_spaces_is_still_flagged_when_genuinely_moded(tmp_path):
    """The other direction of the same fix — it must not go blind to the name.

    Hardening a parse by ignoring awkward input is the failure that looks like a
    fix. A space-bearing name that IS in a mode is still a stranded operator.
    """
    _, body, _ = _run(tmp_path, [_pane("my scratch pane", 1, "tree-mode")])
    assert "ANOMALY moded-destination" in body, body


def test_an_ATTACHED_moded_pane_is_never_touched(tmp_path):
    """THE SAFETY BOUNDARY. Somebody is looking at this pane right now.

    Clearing it would yank a live client out of a selection it is part way
    through — the destroys-something failure the lobby door was rebuilt to end.
    It also costs no coverage to skip: the bug's precondition is that the mode
    SURVIVED A DISCONNECT, so the pane that strands an operator always reads
    attached=0.

    Without this test, widening the predicate to "any moded dest pane" passes
    every other test in this file.
    """
    _, body, _ = _run(tmp_path, [_pane("cc-2", 1, "tree-mode", attached=1)])
    assert "ANOMALY" not in body, body
    assert "CLEARED" not in body, body


def test_an_unattached_moded_pane_is_still_selected_beside_an_attached_one(tmp_path):
    """The discriminating case for the attached rule, in the other direction.

    A predicate that bailed on seeing any attached pane — or that read the
    wrong field — would pass the test above while going blind to the real one.
    """
    _, body, _ = _run(
        tmp_path,
        [
            _pane("cc-2", 1, "tree-mode", attached=1, pane_id="%1"),
            _pane("cc-3", 1, "tree-mode", attached=0, pane_id="%2"),
        ],
    )
    assert body.count("ANOMALY") == 1, body
    assert "cc-3:1.0" in body, body
    assert "cc-2:1.0" not in body.split("pane ")[0], body


def test_the_entry_label_is_recorded(tmp_path):
    """Both doors call this; the log has to say which one, or a slot-door
    occurrence is indistinguishable from a picker occurrence."""
    _, body, _ = _run(tmp_path, [_pane("cc-1", 0)], label="slot-4")
    assert "entry=slot-4" in body, body


def test_it_never_fails_its_caller(tmp_path):
    """It runs on the login path ahead of an `exec`. A non-zero exit or a crash
    would break the operator's way in, over a diagnostic."""
    proc, _, _ = _run(tmp_path, [_pane("cc-1", 0)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"must stay silent on the terminal: {proc.stdout!r}"


def test_empty_pane_data_writes_nothing_rather_than_an_empty_record(tmp_path):
    """No server, or every read timed out. A cold first connection lands here
    legitimately, so it is silence — not a record claiming an empty fleet."""
    proc, body, logs = _run(tmp_path, [])
    assert proc.returncode == 0
    assert logs == [], "an unreadable fleet must not be logged as an empty one"
    assert body == ""


def test_the_log_is_owner_only(tmp_path):
    """It names sessions and command basenames. Not secrets, but not world
    readable either — the sibling cc_exit_capture.sh holds the same line."""
    _, _, logs = _run(tmp_path, [_pane("cc-1", 0)])
    assert logs, "expected a log file"
    assert oct(logs[0].stat().st_mode)[-3:] == "600", oct(logs[0].stat().st_mode)


def test_repeated_entries_append_rather_than_truncate(tmp_path):
    """Two connections in one day must both survive; the bug is intermittent and
    the previous entry is often the one that holds the evidence."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text(_pane("cc-1", 0) + "\n")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GENESIS_FLEET_GUARD_PANES_FILE"] = str(panes_file)
    env.pop("TMUX", None)
    for _ in range(2):
        subprocess.run(["bash", str(GUARD), "lobby"], env=env, check=True, timeout=60)
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    assert len(logs) == 1
    assert logs[0].read_text().count("entry=lobby") == 2


def test_a_wedged_tmux_cannot_hold_the_login_for_the_sum_of_the_reads(tmp_path):
    """The budget is an AGGREGATE deadline, not a per-read belt.

    This is behavioural on purpose. A test asserting that a 5s constant exists
    passes just as happily against three serial reads carrying 5s each — and
    that shape was the original, MEASURED at 15s with a stubbed hanging tmux
    against the 5s it advertised. The operator pays the SUM, and only elapsed
    wall time can tell the two apart.

    The threshold is loose (10s) rather than tight: the fixed path finishes in
    ~5s and the regression cannot finish before 15s, so 10s sits in the gap with
    room on both sides. It is deliberately NOT asserting "about 5 seconds",
    which would flake on a loaded box for no added signal.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "tmux"
    stub.write_text("#!/bin/sh\nsleep 30\n")
    stub.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["HOME"] = str(home)
    env.pop("TMUX", None)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)

    start = time.monotonic()
    proc = subprocess.run(
        ["bash", str(GUARD), "lobby"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 10, (
        f"capture took {elapsed:.1f}s against a hanging tmux; the whole-run "
        "budget is 5s. A per-read belt would land near 15s."
    )


# --------------------------------------------------------------------------
# End-to-end against a REAL tmux server. Skipped only where tmux is absent;
# the classification tests above carry the logic coverage regardless.
# --------------------------------------------------------------------------

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed on this runner"
)


@pytestmark_tmux
def test_end_to_end_against_a_real_tmux_server(tmp_path):
    """Drives the real tmux path — no injected seam — on an ISOLATED socket.

    Isolation matters and is easy to get wrong: TMUX_TMPDIR alone does NOT
    isolate when ``$TMUX`` is inherited (tmux resolves the server from ``$TMUX``
    first), so the env below clears it. Getting this wrong creates sessions on
    the operator's live fleet.
    """
    sock_dir = tmp_path / "s"
    sock_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(sock_dir)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)
    env["HOME"] = str(home)

    def tmux(*args, check=True):
        return subprocess.run(
            ["tmux", *args],
            env=env,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    try:
        tmux("new-session", "-d", "-s", "cc-8", "sleep", "60")
        # Guard-the-guard: the fixture must actually create the hazard, or a
        # passing assertion below would mean nothing.
        tmux("choose-tree", "-Zs", "-t", "cc-8", check=False)
        state = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_in_mode}").stdout
        if "cc-8 1" not in state:
            pytest.skip(f"tmux build did not enter the mode: {state!r}")

        pane_pid_before = tmux(
            "list-panes", "-a", "-F", "#{pane_pid} #{pane_current_command}"
        ).stdout.strip()

        subprocess.run(["bash", str(GUARD), "lobby"], env=env, check=True, timeout=60)
        logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
        assert logs, "guard wrote no log against a live server"
        body = logs[0].read_text()
        # Match the SESSION, never a window/pane index: `base-index` is tmux
        # configuration and differs between a configured fleet (1) and a
        # config-less isolated server (0). Pinning the index here made this test
        # pass or fail on the runner's tmux.conf rather than on the code.
        assert "ANOMALY moded-destination cc-8:" in body, body

        # THE POINT OF THE FEATURE: the pane is no longer stuck. Asserting the
        # log line alone would pass against a guard that reports perfectly and
        # repairs nothing — which is exactly what this branch used to be.
        assert "CLEARED" in body, body
        assert "CLEAR-FAILED" not in body, body
        after = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_in_mode}").stdout
        assert "cc-8 0" in after, f"pane still in a mode after the guard: {after!r}"

        # ...and the clear was NON-DESTRUCTIVE. The doors' whole contract is
        # that nothing is lost; a "fix" that respawned the pane would satisfy
        # every assertion above while killing the operator's work.
        pane_pid_after = tmux(
            "list-panes", "-a", "-F", "#{pane_pid} #{pane_current_command}"
        ).stdout.strip()
        assert pane_pid_after == pane_pid_before, (
            f"the clear disturbed the pane process: {pane_pid_before!r} -> {pane_pid_after!r}"
        )
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, timeout=60)


def _stub_tmux(tmp_path, *, clears: bool) -> Path:
    """A fake `tmux` on PATH that reports one stranded pane.

    With ``clears=False`` it accepts the `copy-mode` call and then still reports
    ``in_mode=1`` on the verification read — the failed-clear case, which the
    real tmux will not produce on demand because `copy-mode -q` works.

    This exists because `copy-mode -q` returns 0 whether or not the pane was in
    a mode, so its exit status can only ever confirm. The verification read is
    the only thing standing between the log and a confident lie.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    witness = tmp_path / "copy_mode_was_called"
    stub = bin_dir / "tmux"
    # THE STUB MUST WITNESS THE CLEAR. An earlier version matched only the two
    # list-panes formats, so `copy-mode` fell through to a bare `exit 0` and the
    # verification read reported in_mode=0 whether or not a clear was ever
    # issued — an adversarial review removed the `copy-mode` call entirely and
    # this stub still printed CLEARED. It now records the call and reports the
    # pane as clear ONLY if it happened, so "a clear was issued" is bound here
    # and not only in the tmux-gated e2e.
    cleared_branch = (
        f'if [ -f "{witness}" ]; then echo "pane=%9 in_mode=0"; '
        'else echo "pane=%9 in_mode=1"; fi; exit 0'
    )
    stub.write_text(
        "#!/bin/sh\n"
        # The clear is issued as `if-shell -F <cond> "copy-mode -q -t %N"`, so
        # tmux evaluates the attachment condition and the clear as ONE command
        # and nothing can attach between them. The witness matches BOTH verbs:
        # keyed on `copy-mode` alone it stopped seeing the clear the moment the
        # call became atomic, which is a stub that silently stops testing the
        # thing it was written for.
        'case "$1" in\n'
        f'  if-shell|copy-mode) : > "{witness}"; exit 0 ;;\n'
        "esac\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    *kind=*) "
        'echo "in_mode=1 kind=dest pane=%9 attached=0 cc-9:1.0 '
        'mode=tree-mode pid=1 cmd=bash"; exit 0 ;;\n'
        "    pane=*in_mode*) "
        + (cleared_branch if clears else 'echo "pane=%9 in_mode=1"; exit 0')
        + " ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir


def _run_with_stub(tmp_path, bin_dir: Path, **extra_env):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(home)
    env.pop("TMUX", None)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)
    env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(GUARD), "lobby"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    return proc, (logs[0].read_text() if logs else "")


def test_the_clear_is_issued_atomically_with_its_attachment_check(tmp_path):
    """The TOCTOU fix, bound by the argv tmux actually receives.

    The shell-side attachment test runs against an inventory taken several tmux
    calls earlier, so a concurrent SSH login can attach inside that window:
    login A classifies %9 as detached, login B attaches, login A cancels B's
    chooser. Re-reading in shell only narrows that. `if-shell -F` makes tmux
    evaluate the condition and run the clear as ONE command, so nothing can
    interleave.

    Asserting the pane ends up clear would NOT catch a revert to a bare
    `copy-mode` — both clear it. Only the issued command distinguishes them.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{argv_log}"\n'
        'case "$1" in\n'
        "  if-shell|copy-mode) exit 0 ;;\n"
        "esac\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    *kind=*) "
        'echo "in_mode=1 kind=dest pane=%9 attached=0 cc-9:1.0 '
        'mode=tree-mode pid=1 cmd=bash"; exit 0 ;;\n'
        '    pane=*in_mode*) echo "pane=%9 in_mode=0"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    _run_with_stub(tmp_path, bin_dir)

    calls = argv_log.read_text() if argv_log.exists() else ""
    clear_calls = [ln for ln in calls.splitlines() if "copy-mode" in ln]
    assert clear_calls, f"no clear was issued at all:\n{calls}"
    for call in clear_calls:
        assert call.startswith("if-shell"), (
            "the clear must be issued through `if-shell -F` so the attachment "
            f"check and the clear cannot be interleaved. Got: {call!r}"
        )
        assert "session_attached" in call, f"`if-shell` carries no attachment condition: {call!r}"


def test_a_clear_that_did_not_work_is_reported_as_failed_not_as_success(tmp_path):
    """The worst outcome this feature could have: operator still stranded, log
    says fixed.

    `copy-mode -q` exits 0 whether or not it cleared anything, so trusting its
    return code would report every attempt as a success. Only the verification
    re-read can tell them apart, and a mutation that reports success without it
    passed every other test in this file.
    """
    _, body = _run_with_stub(tmp_path, _stub_tmux(tmp_path, clears=False))
    assert "CLEAR-FAILED %9" in body, body
    assert "CLEARED %9" not in body, body


def test_a_clear_that_worked_is_reported_as_cleared(tmp_path):
    """The other direction — the same stub, reporting the pane as clear.

    Without this, a guard that reported CLEAR-FAILED unconditionally would pass
    the test above.
    """
    _, body = _run_with_stub(tmp_path, _stub_tmux(tmp_path, clears=True))
    assert "CLEARED %9" in body, body
    assert "CLEAR-FAILED" not in body, body


def test_a_pane_attached_under_ANOTHER_session_is_never_cleared(tmp_path):
    """`list-panes -a` emits one row per (SESSION, pane), and `attached` is a
    property of the SESSION.

    A window linked into a second session puts the SAME pane id on two rows with
    different attached counts. MEASURED on tmux 3.4 with a live client on
    `viewer`: pane %0 appears as `sess=owner attached=0 in_mode=1` AND
    `sess=viewer attached=1 in_mode=1`. Deciding per-ROW matches the first line
    and clears a pane somebody is looking at — the precise failure condition 3
    exists to prevent, found by adversarial review.
    """
    _, body, _ = _run(
        tmp_path,
        [
            _pane("owner", 1, "tree-mode", attached=0, pane_id="%0"),
            _pane("viewer", 1, "tree-mode", attached=1, pane_id="%0"),
        ],
    )
    assert "ANOMALY" not in body, body
    assert "CLEARED" not in body, body


def test_a_shared_pane_that_is_attached_NOWHERE_is_still_cleared(tmp_path):
    """The other direction: the aggregation must not blind the guard.

    The same pane on two rows, neither attached, is genuinely stranded — and
    must be recorded ONCE rather than per row.
    """
    _, body, _ = _run(
        tmp_path,
        [
            _pane("owner", 1, "tree-mode", attached=0, pane_id="%0"),
            _pane("viewer", 1, "tree-mode", attached=0, pane_id="%0"),
        ],
    )
    assert body.count("ANOMALY") == 1, body


def test_a_budget_spent_mid_sweep_says_so_rather_than_going_silent(tmp_path):
    """Running out of time is a third outcome, and it used to be invisible.

    MEASURED by adversarial review: three stranded panes against a stub
    sleeping 3s per clear produced three ANOMALY lines and NO
    CLEARED/CLEAR-FAILED/CLEAR-SKIPPED at all — two panes untouched, the
    verification skipped, and a record that reads like "we looked and chose to
    do nothing".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in if-shell|copy-mode) sleep 3; exit 0 ;; esac\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    *kind=*) "
        'printf "in_mode=1 kind=dest pane=%%1 attached=0 a:1.0 mode=t pid=1 cmd=b\\n'
        "in_mode=1 kind=dest pane=%%2 attached=0 b:1.0 mode=t pid=1 cmd=b\\n"
        'in_mode=1 kind=dest pane=%%3 attached=0 c:1.0 mode=t pid=1 cmd=b\\n"; exit 0 ;;\n'
        "    pane=*in_mode*) exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    _, body = _run_with_stub(tmp_path, bin_dir)

    # THE INVARIANT IS ACCOUNTING, not the presence of a keyword. Asserting
    # only that "CLEAR-INCOMPLETE" appears was too weak: a mutation that
    # stopped recording panes skipped INSIDE the loop still produced the line
    # from a different branch, and the test passed. What must hold is that
    # EVERY stranded pane gets a disposition — cleared, failed, or unreached —
    # because a pane that appears in no line is one the operator is still stuck
    # in while the log looks complete.
    dispositions = " ".join(
        ln
        for ln in body.splitlines()
        if ln.startswith(("CLEARED", "CLEAR-FAILED", "CLEAR-INCOMPLETE"))
    )
    for pane_id in ("%1", "%2", "%3"):
        assert pane_id in dispositions, (
            f"{pane_id} was stranded but appears in no disposition line.\n"
            f"dispositions: {dispositions!r}\nfull log:\n{body}"
        )
    assert "CLEAR-INCOMPLETE" in body, body


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF", "flase"])
def test_any_non_enabling_lever_value_stops_the_clear(tmp_path, value):
    """Degrade toward LESS write authority.

    Only `on/1/true/yes` enable. Everything else set — including a TYPO —
    disables, so a mistyped lever stops the mutation instead of silently
    permitting it. The first version compared against the exact string "off",
    which left `0`, `false` and `no` quietly clearing.
    """
    _, body = _run_with_stub(
        tmp_path,
        _stub_tmux(tmp_path, clears=True),
        GENESIS_FLEET_GUARD_CLEAR=value,
    )
    assert "CLEAR-SKIPPED" in body, f"value={value!r}: {body}"
    assert "CLEARED" not in body, f"value={value!r}: {body}"


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_enabling_lever_values_keep_the_clear(tmp_path, value):
    """The other direction — without this, a lever that disabled unconditionally
    would pass every test above."""
    _, body = _run_with_stub(
        tmp_path,
        _stub_tmux(tmp_path, clears=True),
        GENESIS_FLEET_GUARD_CLEAR=value,
    )
    assert "CLEARED %9" in body, f"value={value!r}: {body}"


def test_an_explicitly_empty_lever_disables_rather_than_reading_as_unset(tmp_path):
    """`GENESIS_FLEET_GUARD_CLEAR=` is SET, so it must disable.

    `${VAR:-}` collapses set-but-empty into the same state as unset, which left
    clearing ENABLED — the exact inverse of the declared fail-closed semantics.
    A blank line in a config template or a half-finished edit produces this.
    Found by external review.
    """
    _, body = _run_with_stub(
        tmp_path, _stub_tmux(tmp_path, clears=True), GENESIS_FLEET_GUARD_CLEAR=""
    )
    assert "CLEAR-SKIPPED" in body, body
    assert "CLEARED" not in body, body


def test_an_empty_lever_in_the_config_file_also_disables(tmp_path):
    """Same rule through the file path: the KEY being present is what counts."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "cc-slot.env").write_text("GENESIS_FLEET_GUARD_CLEAR=\n")
    bin_dir = _stub_tmux(tmp_path, clears=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(home)
    env.pop("TMUX", None)
    env.pop("GENESIS_FLEET_GUARD_CLEAR", None)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)
    subprocess.run(["bash", str(GUARD), "lobby"], env=env, check=True, timeout=60)
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    body = logs[0].read_text() if logs else ""
    assert "CLEAR-SKIPPED" in body, body
    assert "CLEARED" not in body, body


def test_the_seam_works_on_a_runner_with_no_tmux_at_all(tmp_path):
    """The no-tmux portability contract, actually exercised.

    The tmux requirement used to sit ABOVE the seam check, so on a runner
    without tmux the script exited before reading the injected pane file and
    every "runs anywhere" classification test silently got no log — the tests
    passed for the wrong reason, proving nothing. Found by external review.
    """
    home = tmp_path / "home"
    home.mkdir()
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text(_pane("cc-3", 1, "tree-mode") + "\n")
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    for tool in (
        "bash",
        "date",
        "mkdir",
        "cat",
        "grep",
        "sed",
        "chmod",
        "tr",
        "printf",
        "getent",
        "id",
        "tail",
        "command",
        "flock",
    ):
        src = shutil.which(tool)
        if src:
            (empty_bin / tool).symlink_to(src)

    env = dict(os.environ)
    env["PATH"] = str(empty_bin)  # no tmux on PATH at all
    env["HOME"] = str(home)
    env["GENESIS_FLEET_GUARD_PANES_FILE"] = str(panes_file)
    env.pop("TMUX", None)
    assert shutil.which("tmux", path=str(empty_bin)) is None, "fixture still has tmux"

    proc = subprocess.run(
        ["bash", str(GUARD), "lobby"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    assert logs, f"no log written on a tmux-less runner: {proc.stderr!r}"
    assert "ANOMALY moded-destination cc-3:1.0" in logs[0].read_text()


def test_a_failed_pane_read_is_not_treated_as_a_complete_inventory(tmp_path):
    """A truncated read must be DISCARDED, not used.

    Every nonzero tmux result used to be swallowed, so stdout emitted before a
    connection error was accepted as a full inventory. If that partial output
    holds a linked pane's unattached row but omits its attached one, the
    aggregation calls the pane stranded and clears a mode a live client is
    using. Found by external review; this is the safety boundary, not tidiness.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tmux"
    # Emits a plausible stranded row, then FAILS — the shape of a server that
    # died partway through writing its answer.
    stub.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    *kind=*) "
        'echo "in_mode=1 kind=dest pane=%9 attached=0 cc-9:1.0 '
        'mode=tree-mode pid=1 cmd=bash"; exit 1 ;;\n'
        "  esac\n"
        "done\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    _, body = _run_with_stub(tmp_path, bin_dir)
    assert "CLEARED" not in body, body
    assert "ANOMALY" not in body, (
        "a failed read was accepted as an inventory and drove a decision:\n" + body
    )


def test_a_failed_verification_read_is_recorded_not_assumed_clear(tmp_path):
    """If the verification read fails, nothing knows whether the clear took.

    Claiming success from a read that did not happen is the confident lie the
    verification step exists to prevent, and the accounting invariant says
    every stranded pane gets a disposition.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in if-shell|copy-mode) exit 0 ;; esac\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    *kind=*) "
        'echo "in_mode=1 kind=dest pane=%9 attached=0 cc-9:1.0 '
        'mode=tree-mode pid=1 cmd=bash"; exit 0 ;;\n'
        "    pane=*in_mode*) exit 1 ;;\n"  # verification read FAILS
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    _, body = _run_with_stub(tmp_path, bin_dir)
    assert "CLEARED %9" not in body, body
    assert "CLEAR-INCOMPLETE" in body, body
    assert "%9" in body, body


def test_the_tmux_timeout_carries_a_kill_after_bound(tmp_path):
    """A caught SIGTERM must not outlive the advertised deadline.

    GNU timeout's own documentation notes TERM may be caught or ignored, in
    which case a plain `timeout` waits past its deadline — and here that holds
    BOTH ssh entry paths open. cc-slot.sh's login-path probes already carry
    --kill-after for the same reason.
    """
    text = GUARD.read_text()
    timeout_lines = [
        ln
        for ln in text.splitlines()
        if "timeout " in ln and "command -v" not in ln and not ln.lstrip().startswith("#")
    ]
    assert timeout_lines, "no timeout invocation found at all"
    for ln in timeout_lines:
        assert "--kill-after" in ln, f"unbounded timeout invocation: {ln!r}"


def test_the_lever_can_be_set_in_the_file_the_doors_actually_read(tmp_path):
    """THE BLOCKER: a lever nobody can pull is not a lever.

    MEASURED: an ssh RemoteCommand does not source .bashrc, sshd here is
    `AcceptEnv LANG LC_*` with PermitUserEnvironment off, and lobby-door.sh
    sources nothing — so an env var alone was unreachable and both docs promised
    a switch that could not be operated. ~/.genesis/cc-slot.env is the file
    cc-slot.sh already reads for its door levers, so the guard reads the key
    from there too.
    """
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "cc-slot.env").write_text(
        "# operator levers\nGENESIS_FLEET_GUARD_CLEAR=off\n"
    )
    bin_dir = _stub_tmux(tmp_path, clears=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(home)
    env.pop("TMUX", None)
    env.pop("GENESIS_FLEET_GUARD_CLEAR", None)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)
    subprocess.run(["bash", str(GUARD), "lobby"], env=env, check=True, timeout=60)
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    body = logs[0].read_text() if logs else ""
    assert "CLEAR-SKIPPED" in body, body
    assert "CLEARED" not in body, body


def test_the_operator_kill_switch_stops_the_clear(tmp_path):
    """Every autonomous behaviour ships an off switch, and an untested switch is
    one that can silently stop working.

    GENESIS_FLEET_GUARD_CLEAR=off must leave the pane alone AND say so — a
    silent no-op would be indistinguishable from a fleet that had nothing wrong
    with it.
    """
    _, body = _run_with_stub(
        tmp_path,
        _stub_tmux(tmp_path, clears=True),
        GENESIS_FLEET_GUARD_CLEAR="off",
    )
    assert "CLEAR-SKIPPED" in body, body
    assert "CLEARED" not in body, body
    # ...but it must still REPORT the stranded pane, or turning the clear off
    # would also blind the diagnostic it was built on.
    assert "ANOMALY moded-destination" in body, body


@pytestmark_tmux
def test_end_to_end_an_attached_pane_survives_the_guard(tmp_path):
    """The safety boundary, against a REAL server rather than a fixture.

    The seam-driven test asserts the guard does not FLAG an attached pane. This
    asserts it does not CLEAR one — a different failure, reachable only through
    the real tmux path, and the one that would cost an operator their place in
    a live selection.
    """
    sock_dir = tmp_path / "s"
    sock_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(sock_dir)
    env.pop("GENESIS_FLEET_GUARD_PANES_FILE", None)
    env["HOME"] = str(home)

    def tmux(*args, check=True):
        return subprocess.run(
            ["tmux", *args],
            env=env,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    client = None
    try:
        tmux("new-session", "-d", "-s", "cc-8", "sleep", "60")
        # A real attached client, via a pty — `session_attached` counts clients,
        # so there is no way to fake this one. `tmux attach` refuses to run
        # under a missing or `dumb` TERM (measured: exits before the server
        # registers the client, leaving session_attached=0 and this test
        # silently skipping), so pin a real terminal type for the client only.
        attach_env = dict(env)
        attach_env["TERM"] = "xterm"
        client = subprocess.Popen(
            ["script", "-qec", "tmux attach -t cc-8", "/dev/null"],
            env=attach_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Poll rather than sleep(2): the attach handshake is async, and a fixed
        # nap races it on a loaded runner.
        for _ in range(50):
            state = tmux(
                "list-panes", "-a", "-F", "#{pane_in_mode} #{session_attached}"
            ).stdout
            if "0 1" in state:
                break
            if client.poll() is not None:
                break
            time.sleep(0.2)
        if "0 1" not in state:
            pytest.skip(
                f"attach client never registered "
                f"(client rc={client.poll()}): {state!r}"
            )
        tmux("choose-tree", "-Zs", "-t", "cc-8", check=False)
        state = tmux("list-panes", "-a", "-F", "#{pane_in_mode} #{session_attached}").stdout
        if "1 1" not in state:
            pytest.skip(f"fixture did not produce a moded ATTACHED pane: {state!r}")

        subprocess.run(["bash", str(GUARD), "lobby"], env=env, check=True, timeout=60)
        logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
        body = logs[0].read_text() if logs else ""
        assert "CLEARED" not in body, body
        after = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_in_mode}").stdout
        assert "cc-8 1" in after, f"the guard cleared a pane with a live client attached: {after!r}"
    finally:
        if client is not None:
            client.kill()
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, timeout=60)
