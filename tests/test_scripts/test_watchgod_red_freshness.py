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
import stat
import subprocess
import sys
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

_PRELUDE = (
    'CC_TMP_BUDGET_MB=4; queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
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
    assert "in-flight guard UNAVAILABLE" in log, (
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

    snippet = (
        _PRELUDE
        + f'live_open_paths() {{ printf "%s\\n" "{wheel}"; '
        + 'for i in $(seq 1 20000); do echo "/noise/path/number-$i/file.bin"; done; }; '
        + "clean_cc_red"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert unpack.is_dir() and wheel.exists(), (
        "a large /proc snapshot made a FOUND writer read as absent "
        "(SIGPIPE 141 via pipefail) and the directory was reaped"
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
    assert "in-flight guard UNAVAILABLE" in log, (
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

    proc = _run(home, bind, "fs_total_mb() { echo 1000; }; cc_tmp_capacity_mb")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "1000", (
        f"min() did not pick the truthful smaller statfs total: {proc.stdout!r}"
    )

    proc = _run(home, bind, 'CC_TMP_CAPACITY_MB="2G"; cc_tmp_capacity_mb')
    assert proc.returncode == 0, f"a non-numeric CC_TMP_CAPACITY_MB killed the shell: {proc.stderr}"
    assert proc.stdout.strip() == "2048", (
        f"garbage config did not degrade to the default: {proc.stdout!r}"
    )
