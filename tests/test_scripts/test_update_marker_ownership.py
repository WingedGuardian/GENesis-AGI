"""Owner-checked marker deletes in scripts/update.sh (deploy-audit P5-B, part 4).

update.sh must NOT unconditionally delete ~/.genesis/update_in_progress.pid: on
the supervised path the orchestrator holds it with ITS pid across tiers, and
scripts/restore.sh holds it with its own pid while rebuilding the DB — stripping
a live foreign holder's marker reopens the watchdog-revives-mid-op hazard. The
`_clear_deploy_state` helper deletes the marker only if WE own it ($$) OR its
holder is dead. The state file is always removed (update.sh wrote it).

These drive the ACTUAL shipped `_clear_deploy_state` function against each marker
state.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
# update.sh sources this before defining _clear_deploy_state.
MARKER_LIB = REPO_ROOT / "scripts" / "lib" / "deploy_marker.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def _extract_func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name} not found"
    return f"{name}() {{\n{m.group(1)}\n}}"


def test_no_unconditional_marker_rm_remains(text: str) -> None:
    """Every marker delete must go through the owner-checked helper — no raw
    `rm -f ... update_in_progress.pid` outside `_clear_deploy_state`."""
    body = _extract_func(text, "_clear_deploy_state")
    outside = text.replace(body, "")
    # No raw DELETE of the marker may survive outside the helper.
    assert not re.search(r"rm -f[^\n]*update_in_progress\.pid", outside), (
        "a raw marker rm survives outside the helper"
    )
    assert text.count("_clear_deploy_state\n") >= 5, "helper must be called at every cleanup site"
    # The direct path no longer adopts/writes a marker (it signals via the state
    # file), so update.sh must NOT reference the marker outside the helper at all.
    assert "update_in_progress.pid" not in outside, (
        "no marker use should survive outside the helper"
    )


def _run_clear(
    tmp_path: Path, text: str, marker_pid: str | None, marker_mtime: float | None = None
) -> tuple[bool, bool]:
    """Run the shipped _clear_deploy_state with a given marker state.
    Returns (marker_still_exists, state_still_exists)."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    marker = home / ".genesis" / "update_in_progress.pid"
    state = tmp_path / "update_state.json"
    state.write_text("{}")
    if marker_pid is not None:
        marker.write_text(marker_pid)
        if marker_mtime is not None:
            os.utime(marker, (marker_mtime, marker_mtime))
    harness = f"""#!/bin/bash
set -Eeuo pipefail
STATE_FILE="{state}"
. "{MARKER_LIB}"
{_extract_func(text, "_clear_deploy_state")}
_clear_deploy_state
"""
    script = tmp_path / "h.sh"
    script.write_text(harness)
    subprocess.run(
        ["bash", str(script)], env={**os.environ, "HOME": str(home)}, timeout=10, check=True
    )
    return marker.exists(), state.exists()


def test_deletes_marker_we_own(tmp_path: Path, text: str) -> None:
    # A helper subshell's $$ differs from any pid we write, so use the harness's
    # own pid by writing "$$" — emulate ownership by writing the bash pid at run.
    # Simpler: a marker holding THIS python process's pid is a live FOREIGN pid;
    # to test "owned", we write the shell's own $$ from inside the harness.
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    state = tmp_path / "s.json"
    state.write_text("{}")
    marker = home / ".genesis" / "update_in_progress.pid"
    harness = f"""#!/bin/bash
set -Eeuo pipefail
STATE_FILE="{state}"
echo "$$" > "{marker}"      # marker holds OUR pid → owned
. "{MARKER_LIB}"
{_extract_func(text, "_clear_deploy_state")}
_clear_deploy_state
"""
    (tmp_path / "h.sh").write_text(harness)
    subprocess.run(
        ["bash", str(tmp_path / "h.sh")],
        env={**os.environ, "HOME": str(home)},
        timeout=10,
        check=True,
    )
    assert not marker.exists(), "an owned marker must be deleted"
    assert not state.exists(), "the state file is always removed"


def test_keeps_live_foreign_marker(tmp_path: Path, text: str) -> None:
    """A LIVE foreign holder's marker (e.g. a concurrent restore) must survive."""
    # os.getpid() is this pytest process — alive and NOT the harness's $$.
    marker_exists, state_exists = _run_clear(tmp_path, text, str(os.getpid()))
    assert marker_exists, "a live foreign marker must NOT be stripped"
    assert not state_exists, "the state file is still removed"


def test_deletes_dead_marker(tmp_path: Path, text: str) -> None:
    """A marker whose holder is dead (a stale direct-run systemd-run pid) is cleaned."""
    # PID 2^31-ish is not a live process.
    marker_exists, _ = _run_clear(tmp_path, text, "2147480000")
    assert not marker_exists, "a dead-holder marker must be cleaned"


def test_no_marker_is_safe(tmp_path: Path, text: str) -> None:
    marker_exists, state_exists = _run_clear(tmp_path, text, None)
    assert not marker_exists and not state_exists


@pytest.fixture
def zombie_pid():
    """The pid of a child that has exited and that its parent never reaps: a
    zombie, on which `kill -0` still succeeds."""
    import sys
    import time

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, time\npid = os.fork()\nif pid == 0:\n    os._exit(0)\n"
            "print(pid, flush=True)\ntime.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        zpid = int(holder.stdout.readline())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{zpid}/stat").read_text()
            if stat[stat.rindex(")") + 2] == "Z":
                break
            time.sleep(0.05)
        assert subprocess.run(["kill", "-0", str(zpid)]).returncode == 0, "control: kill -0 lies"
        yield zpid
    finally:
        holder.kill()
        holder.wait()


def test_deletes_a_zombie_holders_marker(tmp_path: Path, text: str, zombie_pid: int) -> None:
    """A killed holder its parent never reaped is dead, though `kill -0` still
    succeeds on it (round-2 review on #2494): the stale marker is cleaned."""
    marker_exists, _ = _run_clear(tmp_path, text, str(zombie_pid))
    assert not marker_exists, "a zombie holder's marker must be cleaned"


def test_keeps_a_live_holders_marker_however_old(tmp_path: Path, text: str) -> None:
    """No clock comparison: a live holder's marker whose mtime is far behind the
    holder's start (what a forward clock step produces) is kept."""
    marker_exists, _ = _run_clear(tmp_path, text, str(os.getpid()), marker_mtime=1_000_000_000)
    assert marker_exists, "a live holder's marker must survive a clock step"


# ── The acquire side: restore.sh takes the marker through
# _acquire_deploy_marker, a different call site from update.sh's cleanup above.


def _run_acquire(
    tmp_path: Path, marker_pid: str, marker_mtime: float | None = None
) -> tuple[int, str]:
    """Source the shipped lib and acquire over a marker holding `marker_pid`.
    Returns the exit status and what the marker holds afterwards, with the
    harness's own pid written as the literal `SELF`."""
    home = tmp_path / "home"
    marker = home / ".genesis" / "update_in_progress.pid"
    marker.parent.mkdir(parents=True)
    marker.write_text(marker_pid + "\n")
    if marker_mtime is not None:
        os.utime(marker, (marker_mtime, marker_mtime))
    script = tmp_path / "acquire.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"source {MARKER_LIB}\n"
        "rc=0; _acquire_deploy_marker || rc=$?\n"
        'held="$(cat "$DEPLOY_MARKER_FILE")"\n'
        '[ "$held" = "$$" ] && held=SELF\n'
        'echo "$rc $held"\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    out = subprocess.run(
        ["bash", str(script)],
        env={**env, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()
    return int(out[0]), out[1]


def test_acquire_refuses_a_live_holder(tmp_path: Path) -> None:
    """Control for the zombie case below: a live holder (this pytest process) is
    refused and left in place."""
    rc, held = _run_acquire(tmp_path, str(os.getpid()))
    assert (rc, held) == (1, str(os.getpid()))


def test_acquire_replaces_a_zombie_holder(tmp_path: Path, zombie_pid: int) -> None:
    rc, held = _run_acquire(tmp_path, str(zombie_pid))
    assert (rc, held) == (0, "SELF"), "a zombie holder is stale; the marker is taken"


def test_acquire_refuses_a_live_holder_however_old_its_marker(tmp_path: Path) -> None:
    """A clock step must not hand a live holder's marker to a second deploy."""
    rc, held = _run_acquire(tmp_path, str(os.getpid()), marker_mtime=1_000_000_000)
    assert (rc, held) == (1, str(os.getpid()))


def test_update_sources_the_holder_check_before_its_cleanup_can_run(text: str) -> None:
    """update.sh's _clear_deploy_state calls _deploy_marker_holder_live, which only
    the lib defines. Without the lib, the call fails with status 127, `!` turns that
    into "dead", and the cleanup deletes a LIVE foreign holder's marker. The tests
    above source the lib themselves, so they cannot see a dropped source line; this
    pins the line, and its position ahead of every call of the cleanup."""
    source_at = text.find('. "$SCRIPT_DIR/lib/deploy_marker.sh"')
    assert source_at != -1, "update.sh no longer sources scripts/lib/deploy_marker.sh"
    calls = [m.start() for m in re.finditer(r"^\s+_clear_deploy_state\s*$", text, re.M)]
    assert calls, "no call of _clear_deploy_state found; update this test"
    assert source_at < min(calls), "the lib must be sourced before the cleanup can run"
    assert "_deploy_marker_holder_live()" in MARKER_LIB.read_text()


def _acquire_where_the_marker_cannot_be_written(tmp_path: Path, block: str) -> list[str]:
    """Acquire when the marker cannot be written, then report the exit status, the
    held flag and the named holder. `block` makes the write impossible for any uid,
    root included: a DIRECTORY at the marker path (the write fails) or a FILE where
    the state directory should be (the mkdir fails)."""
    home = tmp_path / "home"
    home.mkdir()
    if block == "marker-is-a-directory":
        (home / ".genesis" / "update_in_progress.pid").mkdir(parents=True)
    else:
        (home / ".genesis").write_text("not a directory\n")
    script = tmp_path / "acquire.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"source {MARKER_LIB}\n"
        "rc=0; _acquire_deploy_marker || rc=$?\n"
        'echo "$rc $_DEPLOY_MARKER_HELD holder=${DEPLOY_MARKER_HOLDER:-none}"\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    return subprocess.run(
        ["bash", str(script)],
        env={**env, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()


@pytest.mark.parametrize("block", ["marker-is-a-directory", "state-dir-is-a-file"])
def test_acquire_fails_when_the_marker_cannot_be_written(tmp_path: Path, block: str) -> None:
    """An unwritten marker must never read as held. The watchdog defers only on a
    marker it can read, so a deploy that went ahead without one could have its
    server restarted mid-deploy. Status 2 separates this from a live foreign
    holder (status 1), which is the only case that names a holder."""
    assert _acquire_where_the_marker_cannot_be_written(tmp_path, block) == [
        "2",
        "false",
        "holder=none",
    ]
