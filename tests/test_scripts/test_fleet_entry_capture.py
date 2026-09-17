"""Tests for scripts/fleet_entry_capture.sh — the fleet pane-mode capture.

WHAT IS UNDER TEST, and why it is worth pinning
-----------------------------------------------
An operator intermittently lands in a "frozen session with a yellow line". The
yellow is tmux's ``mode-style``: the pane is in a MODE (``choose-tree``), not
frozen. ``scripts/lobby-door.sh`` fixed that for the LOBBY pane by giving every
connection its own throwaway picker; it never reaches the panes the picker
SELECTS, and a ``cc-*`` slot left in a mode holds it indefinitely.

The capture records that state at entry so the next occurrence is a recorded
fact rather than a memory. Its one piece of judgment is deciding which moded
pane is an ANOMALY — and that judgment has a trap: the doors' OWN transient
pickers are named ``lobby-<pid>`` and are in tree-mode BY DESIGN. Flagging those
would make every single connection emit an anomaly, which is indistinguishable
from flagging nothing.

Two layers here, deliberately:

* the classification tests drive the real script through an injectable pane
  source, so they run on ANY runner including one with no tmux — a skip guard
  there would let CI prove nothing about the only logic in the file;
* the end-to-end test drives a REAL tmux server on an isolated socket, and is
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
CAPTURE = REPO_ROOT / "scripts" / "fleet_entry_capture.sh"


def _pane(name: str, in_mode: int, mode: str = "", pid: int = 4242) -> str:
    """One synthetic `list-panes` line in the exact format the script requests.

    Field order is the point. The in-mode FLAG and tmux's own picker/dest
    verdict lead, because a session name is free text that may contain SPACES —
    a parse that searched the whole line could be forged by a crafted name.
    `picker` is what `#{?#{m:lobby-*,…},picker,dest}` yields for a
    `lobby-<pid>` session, so this reproduces tmux's verdict rather than
    re-deriving the glob in Python.
    """
    kind = "picker" if name.startswith("lobby-") else "dest"
    return f"in_mode={in_mode} kind={kind} {name}:1.0 mode={mode} attached=0 pid={pid} cmd=bash"


def _run(tmp_path: Path, panes: list[str], label: str = "lobby"):
    """Run the capture with pane data injected, in an isolated HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("\n".join(panes) + "\n" if panes else "")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GENESIS_FLEET_CAPTURE_PANES_FILE"] = str(panes_file)
    env.pop("TMUX", None)

    proc = subprocess.run(
        ["bash", str(CAPTURE), label],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
    body = logs[0].read_text() if logs else ""
    return proc, body, logs


def test_both_doors_actually_invoke_the_capture():
    """Wiring, not existence — the gap an adversarial review found by DELETING
    both call sites and watching 102 tests pass anyway.

    A capture nothing calls is a diagnostic that never runs, and every
    classification test in this file would stay green. Both entries are asserted
    because they are separate exposures: the picker (`-lobby`) is what the
    operator reports, and the numeric slot door (`-<N>`) reaches the same
    hazard through `new-session -A`, which attaches to an existing pane without
    clearing its mode.

    A FIRST VERSION OF THIS TEST WAS VACUOUS and a mutation caught it: it
    grepped each door for the string "fleet_entry_capture.sh", which still
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
            if "fleet_entry_capture.sh" in ln
            and not ln.lstrip().startswith("[")
            and "-x " not in ln
        ]
        assert invocations, (
            f"scripts/{door} has no line that EXECUTES the fleet entry capture "
            "(a comment or an [ -x ] guard mentioning it is not a call)"
        )


def test_the_doors_resolve_the_capture_without_assuming_a_fixed_repo_root():
    """Generalizability: both doors must work on a clone that is not ~/genesis.

    cc-slot.sh hardcodes GENESIS_ROOT="${HOME}/genesis" for its own purposes (a
    separate known defect, deliberately not fixed here) — so the risk is a
    future edit reaching for that variable because it is right there.
    """
    for door in ("lobby-door.sh", "cc-slot.sh"):
        text = (REPO_ROOT / "scripts" / door).read_text()
        line = next(ln for ln in text.splitlines() if "fleet_entry_capture.sh" in ln)
        assert "GENESIS_ROOT" not in line, (
            f"scripts/{door} resolves the capture from a hardcoded root"
        )


def test_script_is_executable_and_parses():
    assert CAPTURE.exists(), f"{CAPTURE} is missing"
    assert os.access(CAPTURE, os.X_OK), "capture script must be executable"
    subprocess.run(["bash", "-n", str(CAPTURE)], check=True, timeout=60)


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
    env["GENESIS_FLEET_CAPTURE_PANES_FILE"] = str(panes_file)
    env.pop("TMUX", None)
    for _ in range(2):
        subprocess.run(["bash", str(CAPTURE), "lobby"], env=env, check=True, timeout=60)
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
    env.pop("GENESIS_FLEET_CAPTURE_PANES_FILE", None)

    start = time.monotonic()
    proc = subprocess.run(
        ["bash", str(CAPTURE), "lobby"],
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
    env.pop("GENESIS_FLEET_CAPTURE_PANES_FILE", None)
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

        subprocess.run(["bash", str(CAPTURE), "lobby"], env=env, check=True, timeout=60)
        logs = sorted((home / ".genesis" / "logs").glob("fleet_entry_*.log"))
        assert logs, "capture wrote no log against a live server"
        body = logs[0].read_text()
        # Match the SESSION, never a window/pane index: `base-index` is tmux
        # configuration and differs between a configured fleet (1) and a
        # config-less isolated server (0). Pinning the index here made this test
        # pass or fail on the runner's tmux.conf rather than on the code.
        assert "ANOMALY moded-destination cc-8:" in body, body
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, timeout=60)
