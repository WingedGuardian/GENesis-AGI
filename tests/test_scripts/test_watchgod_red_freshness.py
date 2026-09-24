"""tmp_watchgod Zone A RED — the reap must spare directories being written.

Origin (measured on a live install, 2026-09-23): `clean_cc_red` deletes every
depth-1 directory except the active session's, with no test for whether
anything is writing into it. `reap_dir_sparing_sockets` is
`find "$1" -depth -not -type s -delete` — its ONLY predicate is "is it a
socket". A `pip install` unpacking a large wheel into `$TMPDIR` (cc-tmp, where
the install's temp policy points it) had its unpack directory removed
mid-download when cc-tmp crossed the RED threshold, and died:

    ERROR: Could not install packages due to an OSError: [Errno 2]
    No such file or directory: '<cc-tmp>/pip-unpack-XXXXXXXX/<wheel>.whl'

THE SIGNAL IS AN OPEN DESCRIPTOR, NOT AN MTIME. A first attempt at this fix
spared anything modified inside the same 60-second window the loose-file sweep
honours, and it was wrong in both directions: a cache written one second ago
and already closed is indistinguishable by mtime from an unpack directory
being written right now, so the guard either destroys in-flight work or
refuses to reclaim fresh junk. It broke two existing socket-sparing tests that
correctly require RED to reclaim freshly-created cache directories. Only a
live file descriptor changes exactly when "something is writing here" changes.

These tests pin BOTH directions, because a guard that spares everything is a
disabled reaper rather than a safer one.

Harness idiom mirrors test_watchgod_socket_sparing.py (deliberately duplicated
— repo precedent: the watchgod test files do not share a conftest).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

# Every capacity/headroom input is stubbed, not just the budget. Unstubbed,
# they measure the real filesystem holding tmp_path — so on a CI runner with a
# small or nearly-full disk the headroom falls under sacred ground, every arm
# below silently takes the OXYGEN FLOOR path (where the exclusions are empty
# for an entirely different reason), and the sparing arms fail while the
# reclaim arms pass VACUOUSLY. Arms that mean to exercise the floor set these
# themselves and do not use _PRELUDE.
_PRELUDE = (
    "CC_TMP_BUDGET_MB=4; CC_TMP_CAPACITY_MB=2048; SACRED_GROUND_MB=150; "
    "fs_total_mb() { echo 0; }; fs_free_mb() { echo 999999; }; "
    'queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
)


def _assert_not_floor(home: Path) -> None:
    """Guard-the-guard: the arm under test must NOT have taken the oxygen-floor
    path, where exclusions are empty by design and a sparing assertion would
    fail (or a reclaim assertion pass) for the wrong reason entirely.
    """
    log_path = home / ".genesis" / "logs" / "tmp_watchgod.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "OXYGEN FLOOR" not in log, (
        "this arm fell through to the oxygen floor, so it proves nothing about "
        f"the guard it is testing:\n{log}"
    )


# Holds a descriptor open, then idles. The sleep only has to outlive one
# clean_cc_red call; the fixture kills it regardless.
_HOLDER = """
import sys, time
f = open(sys.argv[1], "ab")
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(120)
"""


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


@contextlib.contextmanager
def _writer_holding(path: Path):
    """A live process with `path` open for writing — a download in progress.

    Handshakes on stdout so the descriptor is provably open before the body
    runs; without that the sweep can race the holder's startup and the test
    would pass for the wrong reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"partial-download")
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready", "holder never opened the file"
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _fd_visible(path: Path) -> bool:
    """Whether any live process is visibly holding `path` open, by the same
    /proc route the script uses. Guard-the-guard for the fixtures below: if
    this is False the sweep could not have seen the writer either, so a pass
    would prove nothing.
    """
    for fd_dir in Path("/proc").glob("[0-9]*/fd"):
        try:
            entries = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        for fd in entries:
            try:
                if os.readlink(fd) == str(path):
                    return True
            except OSError:
                continue
    return False


def test_red_spares_a_directory_with_a_live_writer(tmp_path):
    """THE INCIDENT REPLAY. A depth-1 directory a live process is writing into
    must survive RED, contents intact.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "pip-unpack-6zswg0wj"
    wheel = unpack / "fsspec-2026.9.0-py3-none-any.whl"

    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(wheel):
        assert _fd_visible(wheel), (
            "the holder's descriptor is not visible in /proc, so the script "
            "could not have seen it either — this test would pass vacuously"
        )
        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        assert unpack.is_dir(), "RED deleted a directory being actively written"
        assert wheel.exists(), "RED deleted the file a live process had open"


def test_red_reclaims_a_directory_with_no_live_writer(tmp_path):
    """The other arm, and the one the first attempt at this fix broke.

    A freshly-created cache directory with nothing holding it open is exactly
    what RED exists to reclaim. Recency must NOT protect it — that was the
    mtime design's defect, and two existing socket-sparing tests encode this
    same requirement.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    cache = cctmp / "pyright-x"
    cache.mkdir()
    (cache / "blob").write_bytes(b"x" * 8192)  # written NOW, no writer alive

    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    assert not _fd_visible(cache / "blob"), "fixture unexpectedly has a live writer"

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_not_floor(home)

    assert not cache.exists(), (
        "RED failed to reclaim a dead cache directory — the guard is keyed on "
        "recency rather than on a live writer, and the reaper is disabled"
    )


def test_red_spares_the_whole_tree_of_a_live_writer(tmp_path):
    """The loose-file sweep has no -maxdepth, so it walks into directories the
    reap loop spared. A quiet file inside an in-flight directory must survive
    too — a partially-deleted tree breaks its writer just as surely as a
    deleted one.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "tmp8sc9kxwu"
    active = unpack / "currently-writing.part"
    quiet = unpack / "already-downloaded.part"

    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(active):
        quiet.write_bytes(b"o" * 4096)
        # age it past the loose sweep's 60s window so the sweep would take it
        old = os.stat(quiet).st_mtime - 600
        os.utime(quiet, (old, old))

        assert _fd_visible(active), "fixture's descriptor is not visible in /proc"

        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        assert quiet.exists(), (
            "the loose-file sweep reached inside a spared directory and deleted its quiet half"
        )
        assert active.exists()


def test_red_logs_loudly_when_the_inflight_guard_cannot_evaluate(tmp_path):
    """Degrading OPEN is the deliberate direction — refusing to reap would let
    cc-tmp fill, which is what kills CC sessions. Degrading SILENTLY is not:
    the operator must be able to tell a sweep that checked from one that could
    not.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "junk").mkdir()
    (cctmp / "junk" / "f").write_bytes(b"z" * 4096)

    # Simulate an unreadable /proc by overriding the snapshot helper.
    snippet = _PRELUDE + "live_open_paths() { :; }; clean_cc_red"
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "in-flight guard DEGRADED" in log, (
        "RED degraded to reaping without the guard and said nothing"
    )
    # Scoped deliberately: this fixture creates no `claude-*` dir, so
    # `newest_session` is empty and the loose-file sweep's
    # `-not -path "$newest_session/*"` becomes `-not -path "/*"`, which matches
    # every absolute path and deletes nothing. The reclaim proven here is the
    # depth-1 REAP LOOP's, not the sweep's. Asserting more than that would be
    # asserting something this fixture cannot exercise.
    assert not (cctmp / "junk").exists(), (
        "degraded RED must still reclaim via the reap loop — failing closed "
        "here would spare everything and let cc-tmp fill"
    )


def test_red_does_not_delete_a_cache_it_just_logged_as_spared(tmp_path):
    """BLOCKER, found by adversarial review 2026-09-23.

    RED reaps depth-1 directories, then deletes `claude-skills` and `tsx-*`
    by name in a SEPARATE sweep twenty lines later. Before the fix that second
    sweep carried no exclusions, so a tsx cache with a live writer was spared
    by the reap loop, LOGGED as spared, and then deleted anyway.

    Two failures in one: the guard did not hold, and the log told the operator
    a directory survived that had not.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    tsx = cctmp / "tsx-abc123"
    compiling = tsx / "compiling.js"

    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(compiling):
        assert _fd_visible(compiling), "fixture's descriptor is not visible in /proc"

        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
        assert tsx.is_dir(), "the by-name cache sweep deleted a directory the reap loop spared"
        # log truthfulness: the spare must actually have been announced
        assert "sparing" in log, "the spare happened but was never logged"


def test_orange_also_spares_a_cache_with_a_live_writer(tmp_path):
    """ORANGE fires at 75% of budget — MORE often than RED — and deletes the
    same two cache names. Guarding only RED leaves the incident class open on
    the tier that actually runs.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    tsx = cctmp / "tsx-def456"
    compiling = tsx / "compiling.js"

    with _writer_holding(compiling):
        assert _fd_visible(compiling)

        proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        assert tsx.is_dir(), "ORANGE deleted a cache directory being written into"


def test_red_still_reclaims_sibling_project_trees(tmp_path):
    """BLOCKER, found by adversarial review 2026-09-23.

    `claude-<uid>/` is a NAMESPACE CONTAINER, not a unit of work — measured on
    a live install it held 7 project trees and 93 session directories, 1 of
    them live. A first version of this fix added that depth-1 parent to the
    exclusion list, which promoted the loose sweep's deliberately narrow
    `-not -path "$newest_session/*"` into `-not -path "<cc-tmp>/claude-1000/*"`
    and stopped 6 of 7 trees being reclaimable at all.

    A guard that spares everything is a disabled reaper, in the direction that
    fills cc-tmp.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    # ORDER MATTERS. `newest_session` is the depth-2 `*/claude-*` directory
    # with the newest mtime, so the dead tree must be built FIRST — built last
    # it becomes "newest" itself and the sweep's own `-not -path
    # "$newest_session/*"` protects the file this test asserts is reclaimed.
    # That is a fixture artefact, not the defect, and it made an earlier
    # version of this test fail on unfixed origin/main for the wrong reason.
    dead_proj = cctmp / "claude-1000" / "proj-dead" / "sess-dead"
    dead_proj.mkdir(parents=True)
    stale = dead_proj / "stale.bin"
    stale.write_bytes(b"s" * 200_000)
    old = os.stat(stale).st_mtime - 600
    os.utime(stale, (old, old))

    live_proj = cctmp / "claude-1000" / "proj-live"
    live_proj.mkdir(parents=True)
    (live_proj / "fresh.bin").write_bytes(b"f" * 1024)

    # guard-the-guard: force the live tree STRICTLY newer — on an mtime tie
    # `sort -rn | head -1` may pick either candidate (round-3 NOTE)
    bump = os.stat(dead_proj.parent).st_mtime + 5
    os.utime(live_proj, (bump, bump))
    assert os.stat(live_proj).st_mtime > os.stat(dead_proj.parent).st_mtime, (
        "fixture inverted: the dead tree is newer, so newest_session would "
        "protect the file this test asserts is reclaimed"
    )

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_not_floor(home)

    assert not stale.exists(), (
        "a stale file in a NON-newest sibling project tree survived — the "
        "exclusion was widened from one session tree to the whole "
        "claude-<uid>/ container, disabling the sweep"
    )


# ── Arms added after the second (security) review round, 2026-09-23 ──────────
# Each pins a MEASURED defect from that round. The fixtures deliberately vary
# the nesting depth and the snapshot size, because every earlier fixture sat at
# the single depth where the previous exclusion design happened to be correct.


def test_red_survives_a_large_snapshot_with_the_writer_first(tmp_path):
    """C1: pipefail + SIGPIPE. With awk exiting on first match, a snapshot
    larger than awk's read buffer leaves printf writing into a closed pipe:
    the pipeline returns 141, the caller reads "no live writer", and a FOUND
    writer produces a reap. Measured boundary ~379KB; this snapshot is ~800KB
    with the writer's path FIRST, the worst case.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "pip-unpack-bigsnap"
    wheel = unpack / "wheel.whl"
    unpack.mkdir()
    wheel.write_bytes(b"w" * 1024)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    # The stub keeps the REAL /proc walk in the middle: the guard now protects
    # from the same snapshot its probe self-test validated, so a stub that
    # emitted only synthetic paths would fail that self-test and the test would
    # measure the degrade path instead of the SIGPIPE behaviour it is about.
    snippet = (
        _PRELUDE
        + f'live_open_paths() {{ printf "%s\\n" "{wheel}"; '
        + "find /proc/[0-9]*/fd -maxdepth 1 -type l -printf '%l\\n' 2>/dev/null || true; "
        + 'for i in $(seq 1 20000); do echo "/noise/path/number-$i/file.bin"; done; }; '
        + "clean_cc_red"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert unpack.is_dir() and wheel.exists(), (
        "a large /proc snapshot made a FOUND writer read as absent "
        "(SIGPIPE 141 via pipefail) and the directory was reaped"
    )

    # The self-test must stay SILENT here, and this assertion is the one that
    # would have caught the defect the piped `grep -q` form carried: `grep -q`
    # exits on first match and SIGPIPEs its feeding printf, so under pipefail
    # the self-test reported FAILURE on any snapshot past the 64KiB pipe buffer
    # — i.e. on every real RED run, while every small fixture passed. Asserting
    # only that the warning APPEARS when blind can never catch that; the
    # healthy direction has to be pinned too.
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "in-flight guard DEGRADED" not in log, (
        "the probe self-test failed on a large but perfectly healthy snapshot — "
        "the guard was silently disabled at exactly the size real snapshots are"
    )


def test_red_spares_a_cache_with_a_deeply_nested_writer(tmp_path):
    """C2: the exclusion named the DEEPEST directory while the cache sweep
    deletes an ANCESTOR by -name. A writer two levels under tsx-* was spared,
    logged as spared, and deleted anyway.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    compiling = cctmp / "tsx-abc123" / "esm" / "chunk" / "compiling.js"
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(compiling):
        assert _fd_visible(compiling)
        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)
        assert (cctmp / "tsx-abc123").is_dir(), (
            "cache sweep deleted an ancestor of a live nested writer"
        )


def test_red_spares_quiet_siblings_of_a_nested_writer(tmp_path):
    """C2, second manifestation: with the writer at depth 2, the quiet file at
    depth 1 of the same work unit was outside the deepest-dir exclusion and the
    loose sweep took it — the original incident one directory level down.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "pip-unpack-nested"
    active = unpack / "sub" / "in-flight.whl"
    quiet = unpack / "already-downloaded.whl"
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(active):
        quiet.write_bytes(b"q" * 4096)
        old = os.stat(quiet).st_mtime - 600
        os.utime(quiet, (old, old))
        assert _fd_visible(active)

        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)
        assert quiet.exists(), "the quiet half of a work unit with a nested writer was deleted"


def test_red_sweep_survives_a_root_level_open_file(tmp_path):
    """C3: CC sets TMPDIR to the cc-tmp ROOT, so ordinary mktemp files live
    there with held descriptors (227 measured live). Deriving the writer's
    directory from such a file yields the root itself, whose exclusion matches
    EVERYTHING — one temp file silently disables the whole sweep.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    rootfile = cctmp / "tmpRootHeld"

    # The reclaim canary must live where ONLY the loose-file sweep reaches:
    # inside claude-<uid>/, which the depth-1 reap loop always skips (it
    # contains newest_session). A depth-1 canary would be reclaimed by the
    # loop even with the sweep disabled, and this test would pass vacuously —
    # the first version of this fixture made exactly that mistake.
    dead = cctmp / "claude-1000" / "proj-dead" / "sess-dead"
    dead.mkdir(parents=True)
    stale = dead / "stale.bin"
    stale.write_bytes(b"x" * 8192)
    old = os.stat(stale).st_mtime - 600
    os.utime(stale, (old, old))

    # newest_session must resolve elsewhere (see the ordering note above)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(rootfile):
        assert _fd_visible(rootfile)
        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)
        assert not stale.exists(), (
            "one held root-level temp file disabled the loose-file sweep — "
            "the only reclaimer for the bulk of cc-tmp"
        )


def test_red_claude_container_unit_is_the_session_not_the_container(tmp_path):
    """The two review rounds' fixes CONFLICT here, and this pins the
    resolution. claude-<uid>/ is a NAMESPACE CONTAINER (measured live: 7
    project trees, 93 session dirs, 1 with live fds). A live fd deep inside it
    must protect ITS SESSION TREE only — excluding the whole container
    (either by depth-1 derivation or by ancestor-chain pruning) turns off
    reclamation for every sibling.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    # dead tree FIRST so newest_session resolves to the live one (see the
    # ordering note on test_red_still_reclaims_sibling_project_trees)
    dead = cctmp / "claude-1000" / "proj-dead" / "sess-dead"
    dead.mkdir(parents=True)
    stale = dead / "stale.bin"
    stale.write_bytes(b"s" * 100_000)
    old = os.stat(stale).st_mtime - 600
    os.utime(stale, (old, old))

    live_out = cctmp / "claude-1000" / "proj-live" / "sess-live" / "task.output"

    with _writer_holding(live_out):
        assert _fd_visible(live_out)
        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        assert live_out.exists(), "the live session's open file was deleted"
        assert not stale.exists(), (
            "a live fd in ONE session tree excluded the whole claude-<uid> "
            "container — sibling project trees stopped being reclaimable"
        )


def test_red_oxygen_floor_bypasses_the_guard_entirely(tmp_path):
    """THE INVARIANT. When true headroom (capacity - used) falls under sacred
    ground, cc-tmp is about to hit its real ceiling — on btrfs backends df
    cannot see that ceiling (measured: df reports the 349GB pool while the
    volume cap is 2GiB). At that point NOTHING is spared: no live descriptor,
    no guard, nothing may block reclamation, because a full cc-tmp is what
    kills CC sessions.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "pip-unpack-doomed"
    wheel = unpack / "wheel.whl"
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(wheel):
        # ~3MB used; capacity 5MB => headroom ~2MB < sacred 4MB => floor fires
        wheel.write_bytes(b"w" * 3_000_000)
        assert _fd_visible(wheel)

        snippet = (
            "CC_TMP_BUDGET_MB=4; CC_TMP_CAPACITY_MB=5; SACRED_GROUND_MB=4; "
            "queue_alert() { :; }; clean_cc_red"
        )
        proc = _run(home, bind, snippet)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

        log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
        assert "OXYGEN FLOOR" in log, (
            "the floor did not announce itself — a bypass this consequential must be loud"
        )
        assert not unpack.exists(), (
            "a live writer was spared BELOW the oxygen floor — the guard "
            "blocked reclamation at the point where cc-tmp is about to die"
        )


def test_yellow_spares_a_live_tmp_file(tmp_path):
    """W3: YELLOW fires at 50% of budget — more often than either tier above
    it — and deletes *.tmp older than 60 minutes with no in-flight check. A
    long download's .tmp file that is 90 minutes old and still OPEN is
    in-flight work by RED's own standard.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    held = cctmp / "long-download.tmp"

    with _writer_holding(held):
        old = os.stat(held).st_mtime - 5400  # 90 minutes
        os.utime(held, (old, old))
        assert _fd_visible(held)

        proc = _run(home, bind, _PRELUDE + "clean_cc_yellow")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)
        assert held.exists(), "YELLOW deleted a .tmp file a live process still had open"


# ── Arms added after round 3, 2026-09-23. The two real findings share one
# generator — name-based special-casing of the claude container — so both get
# pinned, plus the self-test precision and the config-hygiene edges. ─────────


def test_red_claude_skills_cache_is_not_a_container(tmp_path):
    """SF-1: /^claude-/ also matches `claude-skills` — the CACHE this same
    script deletes by name at two tiers — reclassifying it as a session
    container and narrowing its unit to depth 2, so a nested writer's quiet
    sibling was swept: the C2 defect reborn for a neighbouring name.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    active = cctmp / "claude-skills" / "skill-a" / "cloning.pack"
    quiet = cctmp / "claude-skills" / "already-cloned.bin"
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(active):
        quiet.write_bytes(b"q" * 4096)
        old = os.stat(quiet).st_mtime - 600
        os.utime(quiet, (old, old))
        assert _fd_visible(active)

        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)
        assert quiet.exists(), (
            "claude-skills was treated as a session container: its unit "
            "narrowed to depth 2 and the quiet sibling was swept"
        )


def test_red_reclaims_a_dead_claude_skills_cache(tmp_path):
    """The reclaim control for SF-1's fix: with NO writer, the claude-skills
    cache must still be deleted by the by-name sweep — a fix that spared it
    unconditionally would be the disabled-reaper direction again.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    dead = cctmp / "claude-skills"
    (dead / "skill-x").mkdir(parents=True)
    (dead / "skill-x" / "f").write_bytes(b"x" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_not_floor(home)
    assert not dead.exists(), "a dead claude-skills cache was not reclaimed"


def test_red_loose_file_under_container_does_not_exclude_it(tmp_path):
    """SF-2: the root-only F floor missed the container. A held loose file at
    claude-<uid>/lockfile derived the whole container as a unit — the C3
    disabled-reaper one level down. The file itself is protected; the
    container's dead sibling trees stay reclaimable.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    dead = cctmp / "claude-1000" / "proj-dead" / "sess-dead"
    dead.mkdir(parents=True)
    stale = dead / "stale.bin"
    stale.write_bytes(b"s" * 8192)
    old = os.stat(stale).st_mtime - 600
    os.utime(stale, (old, old))

    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)
    lockfile = cctmp / "claude-1000" / "lockfile"

    with _writer_holding(lockfile):
        assert _fd_visible(lockfile)
        proc = _run(home, bind, _PRELUDE + "clean_cc_red")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _assert_not_floor(home)

        assert not stale.exists(), (
            "one held loose file under claude-<uid>/ excluded the whole container from the sweep"
        )
        assert lockfile.exists(), "the held loose file itself was deleted"


def test_red_self_test_detects_a_blind_snapshot(tmp_path):
    """SF-3: the self-test asserted probe visibility but tested only 'anything
    open under cc-tmp' — a prefix match any other descriptor satisfies. A
    snapshot that misses the probe while naming some other cc-tmp path is a
    structurally blind guard, and must warn.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    # A snapshot naming a cc-tmp path that is NOT the probe: the probe is
    # invisible, so the guard cannot see its own descriptors.
    decoy = cctmp / "claude-1000" / "some-session-uuid" / "decoy.out"
    snippet = _PRELUDE + f'live_open_paths() {{ printf "%s\\n" "{decoy}"; }}; clean_cc_red'
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "in-flight guard DEGRADED" in log, (
        "a snapshot that cannot see the guard's own probe passed the "
        "self-test on the strength of an unrelated descriptor"
    )


def test_capacity_helper_is_backend_and_garbage_proof(tmp_path):
    """SF-4/NOTE-2 edges: on an LVM-style backend where statfs tells the truth
    and is SMALLER than the config, min() must pick statfs; a non-numeric
    config value must degrade to the default instead of killing the daemon
    under set -e.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    # BOTH inputs are stubbed in every case below. `_run` copies os.environ, so
    # an inherited CC_TMP_CAPACITY_MB would silently change the first case, and
    # an unstubbed fs_total_mb measures whatever real filesystem holds tmp_path
    # — on a host whose /tmp is smaller than 2048MB the helper would correctly
    # return that smaller number and the assertion would fail for a reason
    # having nothing to do with the helper.
    proc = _run(
        home, bind, "CC_TMP_CAPACITY_MB=2048; fs_total_mb() { echo 1000; }; cc_tmp_capacity_mb"
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "1000", (
        f"min() did not pick the truthful smaller statfs total: {proc.stdout!r}"
    )

    proc = _run(
        home, bind, 'CC_TMP_CAPACITY_MB="2G"; fs_total_mb() { echo 0; }; cc_tmp_capacity_mb'
    )
    assert proc.returncode == 0, f"a non-numeric CC_TMP_CAPACITY_MB killed the shell: {proc.stderr}"
    assert proc.stdout.strip() == "2048", (
        f"garbage config did not degrade to the default: {proc.stdout!r}"
    )

    # ZERO is the dangerous spelling, and it is numeric — a plain ^[0-9]+$ test
    # accepts it. A capacity of 0 makes every headroom negative, which pins the
    # oxygen floor permanently ON and so bypasses the in-flight guard on every
    # RED run: the guard would be off forever, silently.
    proc = _run(home, bind, "CC_TMP_CAPACITY_MB=0; fs_total_mb() { echo 0; }; cc_tmp_capacity_mb")
    assert proc.returncode == 0, f"a zero CC_TMP_CAPACITY_MB killed the shell: {proc.stderr}"
    assert proc.stdout.strip() == "2048", (
        "a capacity of 0 was accepted — every headroom goes negative and the "
        f"in-flight guard is bypassed permanently: {proc.stdout!r}"
    )


# ── Arms added after the first EXTERNAL review round, 2026-09-24 ─────────────
# Two reviewers independently found that the oxygen floor emptied only ONE of
# the three exclusions it claimed to bypass, that the floor was evaluated only
# inside the cleaner (so a budget larger than the volume could keep it
# unreachable), and that the headroom arithmetic ignored what the filesystem
# will actually hand out. Each arm below pins one of those.


def _mksock(path: Path) -> None:
    """A real socket-type inode via mknod — unprivileged, and exactly what
    `find -type s` matches (idiom shared with test_watchgod_socket_sparing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFSOCK | 0o600)


def test_oxygen_floor_bypasses_the_freshness_window_and_the_active_session(tmp_path):
    """Below the floor the guard claimed "nothing is spared" while emptying
    only `live_paths`. Two exclusions survived it: the loose sweep's 60-second
    freshness window, and the active session's own subtree.

    The freshness one is the dangerous half — a root-level file being written
    fast enough to CAUSE the emergency is exactly the file whose mtime is
    always current, so it would survive every sweep all the way to ENOSPC.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    filler = cctmp / "download.tmp"  # mtime NOW: inside the 60s window
    filler.write_bytes(b"f" * 3_000_000)

    session = cctmp / "claude-1000" / "live-session"
    session.mkdir(parents=True)
    session_file = session / "state.json"
    session_file.write_bytes(b"s" * 4096)

    sock = cctmp / "cc-control.sock"
    _mksock(sock)

    # ~3MB used against a 5MB capacity => headroom ~2MB < sacred 4MB.
    snippet = (
        "CC_TMP_BUDGET_MB=4; CC_TMP_CAPACITY_MB=5; SACRED_GROUND_MB=4; "
        "queue_alert() { :; }; clean_cc_red"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "OXYGEN FLOOR" in log, "a bypass this consequential must announce itself"

    assert not filler.exists(), (
        "a file modified seconds ago survived BELOW the oxygen floor — the "
        "freshness window outlived the bypass, and a fast writer would hold "
        "its own exemption all the way to ENOSPC"
    )
    assert not session_file.exists(), (
        "the active session's own tree was spared below the floor, where the "
        "alternative is ENOSPC for that session and every other one"
    )
    assert sock.exists(), (
        "the floor deleted a unix socket: 0 bytes reclaimed, control plane "
        "severed — the one exclusion that must survive the bypass"
    )


def test_red_sweeps_when_no_session_dir_exists(tmp_path):
    """PRE-EXISTING on the default branch. `newest_session` is empty whenever
    cc-tmp holds no claude-<uid>/<project> directory at depth 2, and the sweeps
    spelled their exclusion as an unconditional `-not -path "$newest_session/*"`
    — which expands to `-not -path "/*"`, matching every absolute path. RED
    then runs, logs normally, and reclaims nothing at all.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    stale = cctmp / "orphan-junk.bin"
    stale.write_bytes(b"x" * 4096)
    old = os.stat(stale).st_mtime - 600
    os.utime(stale, (old, old))
    cache = cctmp / "tsx-deadcache"
    cache.mkdir()
    (cache / "chunk.js").write_bytes(b"c" * 512)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_not_floor(home)

    assert not stale.exists(), (
        "RED reclaimed nothing: with no session directory the empty "
        "newest_session made the sweep's -not -path match every path"
    )
    assert not cache.exists(), "the cache sweep was disabled the same way"


def test_check_cc_tmp_reaches_red_on_true_headroom_under_budget(tmp_path):
    """The floor was computed only INSIDE clean_cc_red, so a budget larger than
    the volume kept the only function that evaluates the real ceiling
    unreachable: on btrfs df cannot see the quota, so neither the budget tier
    nor the statfs check would fire while the volume filled.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    snippet = (
        "CC_TMP_BUDGET_MB=2000; CC_TMP_CAPACITY_MB=1024; SACRED_GROUND_MB=150; "
        "dir_usage_mb() { echo 950; }; fs_free_mb() { echo 999999; }; "
        "fs_total_mb() { echo 0; }; "
        'clean_cc_red() { echo "RED-RAN:$1"; }; '
        "clean_cc_orange() { echo ORANGE-RAN; }; clean_cc_yellow() { echo YELLOW-RAN; }; "
        "check_cc_tmp"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    # 950 of a 1024MB volume = 74MB true headroom, under sacred 150 — but only
    # 47% of the 2000MB budget, and df reports plenty free.
    assert "RED-RAN" in proc.stdout, (
        "true headroom of 74MB did not reach RED because the budget was set "
        f"larger than the volume: {proc.stdout!r}"
    )
    assert "RED-RAN:74" in proc.stdout, (
        "the dispatcher did not hand its measured headroom to the cleaner, so "
        f"the two can disagree about which side of the floor they are on: {proc.stdout!r}"
    )
    assert proc.stdout.strip().endswith("red:950"), (
        f"the tier reported to the state file is not red: {proc.stdout!r}"
    )


def test_headroom_is_capped_by_filesystem_free_space(tmp_path):
    """Capacity minus usage can exceed what the filesystem will actually hand
    out — reserved blocks, metadata, and deleted-but-still-open files hold
    space the directory total cannot see. Taking the minimum means a blindness
    in either measure can only make the floor fire EARLIER, never later.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    common = (
        "CC_TMP_CAPACITY_MB=2048; fs_total_mb() { echo 0; }; "
        "dir_usage_mb() { echo 100; }; fs_free_mb() { echo 40; }; cc_tmp_headroom_mb"
    )
    # NB: the body contains a literal `%m` (stat's mount-point format), so this
    # is built by replace() rather than %-formatting or .format().
    stat_stub = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ] && [ "$2" = "%m" ]; then echo "@MOUNT@"; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n'
    )

    # ARM 1 — cc-tmp IS its own mount, so fs_free measures that volume and
    # belongs in the minimum. `stat` is stubbed on PATH, the harness's existing
    # idiom (same as tmux), to report cc-tmp as its own mount point.
    _make_exec(bind / "stat", stat_stub.replace("@MOUNT@", "$3"))
    proc = _run(home, bind, common)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "40", (
        "on a dedicated volume, headroom reported 1948MB of room on a "
        f"filesystem with 40MB free: {proc.stdout!r}"
    )

    # ARM 2 — cc-tmp is NOT its own mount (volume creation was unsupported or
    # failed, or this is a bare-metal install), so fs_free measures the SHARED
    # filesystem and must be ignored. Folding it in would let "the host disk is
    # full" trigger the total-bypass floor inside a near-empty cc-tmp, on every
    # 30s poll, destroying in-flight writes while freeing nothing that moves
    # the host disk. This arm is what keeps that from coming back.
    _make_exec(bind / "stat", stat_stub.replace("@MOUNT@", "/"))
    proc = _run(home, bind, common)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "1948", (
        "a full SHARED filesystem was folded into cc-tmp's headroom — the "
        "oxygen floor would fire and bypass every guard over a disk that "
        f"reclaiming cc-tmp cannot free: {proc.stdout!r}"
    )


_CAP_BLOCK_START = '_cc_cap_gib="${CCTMPVOL_SIZE_GIB:-2}"'


def _extract_capacity_block(script: Path) -> str:
    """The SHIPPED normalization lines, lifted from the writer itself.

    Executing the real lines rather than a copy of them is the point: a test
    that re-implements the arithmetic passes forever while the script drifts.
    """
    lines = script.read_text().split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip() == _CAP_BLOCK_START)
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("_cc_cap_mb="))
    return "\n".join(lines[start : end + 1])


def test_config_writers_normalize_the_volume_size_like_the_volume_lib(tmp_path):
    """Both writers derive CC_TMP_CAPACITY_MB from CCTMPVOL_SIZE_GIB, and that
    value is load-bearing for the oxygen floor. They must accept exactly what
    scripts/lib/cc_tmp_volume.sh's _cctmpvol_size_gib accepts, or the config
    disagrees with the volume that was actually created.

    Two failure directions, both real before this: a non-numeric value hit raw
    shell arithmetic and ABORTED the install under `set -u`, and 0 wrote a
    capacity of 0 — which makes every headroom negative and pins the oxygen
    floor permanently on, bypassing the in-flight guard forever.
    """
    repo = Path(__file__).resolve().parents[2]
    lib = repo / "scripts" / "lib" / "cc_tmp_volume.sh"

    cases = {
        "": 2048,  # unset -> default
        "2": 2048,
        "8": 8192,
        "0": 2048,  # numeric but sub-1
        "abc": 2048,  # would have aborted under set -u
        "2G": 2048,
        "-1": 2048,
    }

    for script in (repo / "scripts" / "bootstrap.sh", repo / "scripts" / "install.sh"):
        block = _extract_capacity_block(script)
        for value, expected_mb in cases.items():
            setter = "" if value == "" else f"CCTMPVOL_SIZE_GIB={value!r}; "
            proc = subprocess.run(
                ["bash", "-c", f'set -euo pipefail\n{setter}{block}\necho "$_cc_cap_mb"'],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            assert proc.returncode == 0, (
                f"{script.name} aborted on CCTMPVOL_SIZE_GIB={value!r}: {proc.stderr}"
            )
            assert proc.stdout.strip() == str(expected_mb), (
                f"{script.name} with CCTMPVOL_SIZE_GIB={value!r} wrote "
                f"{proc.stdout.strip()}MB, expected {expected_mb}MB"
            )

            # …and the SAME value through the volume lib's own helper, so the
            # two cannot drift apart: whatever size the volume is created at is
            # the size the watchdog is told about.
            gib = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\nsource '{lib}'\n{setter}_cctmpvol_size_gib"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            assert gib.returncode == 0, f"volume lib rejected {value!r}: {gib.stderr}"
            assert int(gib.stdout.strip()) * 1024 == expected_mb, (
                f"config writer and volume lib disagree on CCTMPVOL_SIZE_GIB={value!r}: "
                f"lib says {gib.stdout.strip()}GiB, writer says {expected_mb}MB"
            )


def test_red_keeps_the_snapshot_when_the_self_test_fails(tmp_path):
    """A failed self-test must DEGRADE the guard, not disarm it.

    Blanking the exclusions on self-test failure looks conservative and is the
    opposite. Every failure mode the self-test catches — a partial /proc read,
    a CC_TMP_DIR spelling the kernel does not use, a writer owned by another
    uid — makes the snapshot INCOMPLETE, never fictional: a /proc fd link
    cannot name a path nobody has open. So the unverified snapshot is strictly
    better evidence than the empty string, and discarding it throws away the
    real writers it did see.

    The stakes are not hypothetical. The SIGPIPE defect fixed in this same
    change made the self-test fail on every real RED run; under blanking, that
    one false negative would have deleted every live writer's tree instead of
    costing a log line.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    unpack = cctmp / "pip-unpack-degraded"
    wheel = unpack / "wheel.whl"
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    with _writer_holding(wheel):
        assert _fd_visible(wheel), "fixture's descriptor is not visible in /proc"

        # A snapshot that names the real writer but can never contain the
        # probe — so the positive self-test fails while the evidence about the
        # writer is perfectly good.
        snippet = _PRELUDE + f'live_open_paths() {{ printf "%s\\n" "{wheel}"; }}; ' + "clean_cc_red"
        proc = _run(home, bind, snippet)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

        log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
        assert "in-flight guard DEGRADED" in log, (
            "the self-test did not fail, so this arm is not testing the "
            f"degrade path at all:\n{log}"
        )
        assert unpack.is_dir() and wheel.exists(), (
            "a failed self-test discarded a snapshot that named a live writer, "
            "and the writer's directory was reaped — degrading disarmed the "
            "guard instead of weakening it"
        )


def test_config_writers_actually_use_the_normalized_value(tmp_path):
    """Binding, not existence. The arm above proves the normalization block
    COMPUTES the right number; nothing proved the heredoc twenty lines later
    still writes that number. Reverting the config line to the raw arithmetic
    while leaving the (now dead) block in place would keep this file green
    forever — a constant existing is not a constant binding.
    """
    repo = Path(__file__).resolve().parents[2]
    for script in (repo / "scripts" / "bootstrap.sh", repo / "scripts" / "install.sh"):
        text = script.read_text()
        assert re.search(r"^CC_TMP_CAPACITY_MB=\$_cc_cap_mb$", text, re.M), (
            f"{script.name} no longer writes the normalized value into "
            "watchgod.conf — the normalization block above it is dead code and "
            "the raw value reaches the config again"
        )
        assert "_cc_cap_mb=$((" in text, (
            f"{script.name} lost the normalization arithmetic entirely"
        )


def test_red_cache_sweep_preserves_a_socket_inside_a_cache_dir(tmp_path):
    """The cache sweep used `rm -rf`, which has no socket predicate — so a
    socket inside a tsx-*/claude-skills directory was destroyed twenty lines
    after the reap loop deliberately kept its parent BECAUSE it holds a socket.
    That is the spared-then-deleted failure the sweep's own comment says it
    exists to prevent, and it made the floor's "only sockets survive" claim
    false at two of the four places a socket can live.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    in_cache = cctmp / "tsx-abc123" / "cc-in-cache.sock"
    in_skills = cctmp / "claude-skills" / "cc-in-skills.sock"
    at_root = cctmp / "cc-at-root.sock"
    for s in (in_cache, in_skills, at_root):
        _mksock(s)

    # Reclaimable bulk beside each socket, so the sweep has real work to do and
    # the arm cannot pass by the sweep simply not running.
    (cctmp / "tsx-abc123" / "chunk.js").write_bytes(b"c" * 4096)
    (cctmp / "claude-skills" / "pack.bin").write_bytes(b"p" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_not_floor(home)

    for s in (in_cache, in_skills, at_root):
        assert s.is_socket(), (
            f"the cache sweep deleted {s.name}: 0 bytes reclaimed, control "
            "plane severed, and the floor's 'only sockets survive' claim false"
        )
    assert not (cctmp / "tsx-abc123" / "chunk.js").exists(), (
        "socket-sparing turned the cache sweep into a no-op — the reclaimable "
        "content beside the socket must still go"
    )
    assert not (cctmp / "claude-skills" / "pack.bin").exists(), (
        "socket-sparing turned the cache sweep into a no-op in claude-skills"
    )
