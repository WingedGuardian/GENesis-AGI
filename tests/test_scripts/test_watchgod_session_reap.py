"""tmp_watchgod's 7-day session reap — depth and staleness, both of which were wrong.

The YELLOW tier reclaims session workspaces nobody has touched in a week. Until
2026-09-07 it did that with

    find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*/???*" \\
        -mtime +7 -exec rm -rf {} +

and both halves of that line are aimed at the wrong thing.

DEPTH. The layout is ``cc-tmp/claude-<uid>/<project>/<session-uuid>/…``, so
depth 2 is the PROJECT directory. Measured on a live install: one depth-2
directory held 54 session workspaces, including every live session's scratchpad
and the output files of running background tasks. A single ``rm -rf`` took all
of it.

STALENESS. A directory's mtime tracks only its DIRECT children, so a project
directory stops being touched the moment no NEW session starts under it — no
matter how busy the sessions inside are. Measured the same day: the one
actively-used project directory had an mtime 2.1 days old while its contents had
been written seconds earlier, and every dormant project directory showed a
divergence of 0.0 days. The gap appears precisely on the directory that must not
be deleted.

Those two together are the hazard: seven quiet days and a routine 50%-usage
YELLOW — a tier that pages nobody — reaps live workspaces.

Every test here drives ``clean_cc_yellow`` — the tier handler the daemon actually
calls — rather than the reap helper, so they are meaningful against the pre-fix
script instead of merely reporting that a new function name is absent. Measured
verify-RED against ``origin/main``:
``test_live_session_under_a_stale_project_survives`` fails with "the live session
was deleted", which is the hazard demonstrated rather than asserted.

Harness idiom mirrors the sibling watchgod test files (deliberately duplicated —
those files share no conftest): a HOME-redirected sandbox so the script's
HOME-derived paths land in tmp_path, and a tmux stub on a PATH-prepended bin dir.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

_DAY = 86400


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"
    cctmp.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, cctmp, bind


def _run(home: Path, bind: Path, snippet: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _age(path: Path, days: float) -> None:
    """Backdate a path's mtime/atime by ``days``."""
    when = time.time() - days * _DAY
    os.utime(path, (when, when))


def _session(cctmp: Path, project: str, uuid: str) -> Path:
    """Create ``claude-1000/<project>/<uuid>/scratchpad`` and return the session dir."""
    sdir = cctmp / "claude-1000" / project / uuid
    (sdir / "scratchpad").mkdir(parents=True)
    return sdir


def test_live_session_under_a_stale_project_survives(tmp_path):
    """THE hazard, replayed: a project directory whose own mtime is 10 days old
    while a session inside it was written moments ago.

    This is not a constructed curiosity — it is the measured live shape (2.1 days
    of divergence on the only actively-used project directory, and 0.0 on every
    dormant one). The pre-fix depth-2 ``-mtime +7`` sweep matches the project
    directory and ``rm -rf``s the live session with it.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    live = _session(cctmp, "-home-dev-workrepo", "live-uuid")
    (live / "scratchpad" / "notes.json").write_text("fresh work")
    project = cctmp / "claude-1000" / "-home-dev-workrepo"
    _age(project, 10)  # no NEW session started here in 10 days

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert live.exists(), "a live session was reaped because its PROJECT dir went stale"
    assert (live / "scratchpad" / "notes.json").read_text() == "fresh work"
    assert project.exists(), "the project dir must survive while it holds a live session"


def test_stale_session_is_reaped(tmp_path):
    """The feature still works: a session with nothing touched in 7 days goes."""
    home, cctmp, bind = _sandbox(tmp_path)
    old = _session(cctmp, "-home-dev-workrepo", "old-uuid")
    (old / "scratchpad" / "junk.bin").write_bytes(b"x" * 1024)
    for p in (old / "scratchpad" / "junk.bin", old / "scratchpad", old):
        _age(p, 30)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not old.exists(), "a 30-day-old session workspace should be reclaimed"


def test_reap_is_per_session_not_per_project(tmp_path):
    """Two sessions under ONE project, one stale and one fresh: only the stale one
    goes, and the project survives. The pre-fix sweep had no way to express this —
    its unit of deletion was the whole project."""
    home, cctmp, bind = _sandbox(tmp_path)
    fresh = _session(cctmp, "-home-dev-workrepo", "fresh-uuid")
    (fresh / "scratchpad" / "now.txt").write_text("active")
    stale = _session(cctmp, "-home-dev-workrepo", "stale-uuid")
    (stale / "scratchpad" / "then.txt").write_text("done")
    for p in (stale / "scratchpad" / "then.txt", stale / "scratchpad", stale):
        _age(p, 20)
    project = cctmp / "claude-1000" / "-home-dev-workrepo"
    _age(project, 20)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert fresh.exists(), "the fresh session must survive"
    assert not stale.exists(), "the stale sibling should be reclaimed"
    assert project.exists(), "a project dir with a surviving session must remain"


def test_deep_fresh_file_keeps_the_session(tmp_path):
    """Freshness is judged on CONTENTS, recursively — a file several levels down
    counts, even when every directory above it, project directory included, is
    old. Directory mtimes alone cannot see this, which is the whole reason the
    old sweep was wrong."""
    home, cctmp, bind = _sandbox(tmp_path)
    sdir = _session(cctmp, "-home-dev-workrepo", "deep-uuid")
    deep = sdir / "scratchpad" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "just-written.log").write_text("recent")
    # Age every ancestor, project dir included — without that the old depth-2
    # sweep never matched and this test passed against the unfixed code too.
    for p in (
        deep,
        deep.parent,
        deep.parent.parent,
        sdir / "scratchpad",
        sdir,
        cctmp / "claude-1000" / "-home-dev-workrepo",
    ):
        _age(p, 40)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert sdir.exists(), "a fresh file deep inside must keep the session alive"


def test_emptied_project_dir_is_reclaimed_on_a_later_pass(tmp_path):
    """Reaping every session under a project leaves an empty directory behind;
    without cleanup those accumulate forever. Removal is deliberately TWO-PHASE,
    and this pins both halves.

    Removing the last session updates the project directory's own mtime, so
    `-empty -mtime +7` does not match it in the same pass — it goes on a later
    one, once seven quiet days have passed. That delay is the point: CC creates
    `<project>/` and then `<session-uuid>/` as two steps, and an rmdir landing
    between them gives the starting session ENOENT. An empty directory is one
    inode, which is not worth racing a session start for.

    Not regression cover — the old sweep removed the whole project directory, so
    the end state held there too, by deleting live data to get it.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    old = _session(cctmp, "-home-dev-retired", "gone-uuid")
    for p in (old / "scratchpad", old):
        _age(p, 30)
    project = cctmp / "claude-1000" / "-home-dev-retired"
    _age(project, 30)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not old.exists(), "the stale session should be reclaimed"
    assert project.exists(), (
        "the project dir was just touched by that removal — reclaiming it in the "
        "same pass is the race the mtime guard exists to avoid"
    )

    _age(project, 30)  # seven quiet days later
    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not project.exists(), "an empty, long-untouched project dir is reclaimed"


def test_the_reap_spares_a_socket_like_every_other_sweep(tmp_path):
    """The invariant this daemon is built on is "no sweep deletes a socket", and
    a new sweep is a new member of that population.

    A socket's mtime is its BIND time, so a session that bound one inside its
    workspace and then wrote nothing for seven days looks stale — and a plain
    `rm -rf` would have the reap sever a live session in the very sweep added to
    stop that happening. Everything else in the directory is still reclaimed.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    sdir = _session(cctmp, "-home-dev-workrepo", "socket-uuid")
    sock = sdir / "scratchpad" / "agent.sock"
    os.mknod(sock, stat.S_IFSOCK | 0o600)
    junk = sdir / "scratchpad" / "big.bin"
    junk.write_bytes(b"x" * 4096)
    for p in (sock, junk, sdir / "scratchpad", sdir):
        _age(p, 30)
    _age(cctmp / "claude-1000" / "-home-dev-workrepo", 30)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), "the session reap deleted a socket"
    assert not junk.exists(), "everything that is not a socket is still reclaimed"


def test_a_freshly_created_project_dir_is_never_reclaimed(tmp_path):
    """The race itself: a project directory CC created moments ago and is about
    to add a session to must survive a sweep landing in that window."""
    home, cctmp, bind = _sandbox(tmp_path)
    project = cctmp / "claude-1000" / "-home-dev-newrepo"
    project.mkdir(parents=True)  # mkdir <project>, session dir not yet created

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert project.exists(), "a starting session's project dir was deleted under it"


def test_reap_fails_closed_when_the_cutoff_cannot_be_computed(tmp_path):
    """The fail direction, which is the part that turns a housekeeping sweep into
    data loss when it is wrong. If the 7-day cutoff cannot be computed, NOTHING is
    deleted — not even a genuinely ancient session — and the skip is logged.

    Not regression cover (the old sweep called no `date` at all): this is RED
    against the new code with its guard removed, because `find -newermt ""` exits
    0 and treats an empty cutoff as roughly NOW, so every session would read as
    stale and be deleted. The guard is what stands between an unusable timestamp
    and total loss.

    Shadowing `date` with a failing stub is the smallest way to reach that branch;
    the branch itself guards against any environment where the timestamp cannot be
    produced.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    _make_exec(bind / "date", "#!/usr/bin/env bash\nexit 1\n")
    ancient = _session(cctmp, "-home-dev-workrepo", "ancient-uuid")
    for p in (ancient / "scratchpad", ancient):
        _age(p, 400)

    proc = _run(home, bind, "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert ancient.exists(), "an unusable cutoff must delete nothing at all"
