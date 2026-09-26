"""tmp_watchgod socket-sparing — the RED reaper must never delete unix sockets.

Origin (measured on a live install, 2026-09-05): Zone A RED's depth-1 sweep
(`clean_cc_red`) `rm -rf`s every top-level directory of CC_TMP_DIR except the
one holding the newest `*/claude-*` session dir. `cc-socks/` — where the
Claude Code binary binds one unix socket per session for cross-session
messaging — is such a directory and was deleted wholesale, leaving live
sessions listening on bound-but-unlinked sockets: inbound connects fail
ENOENT and the local coordination plane fails silently. The sockets are
0 bytes, so deleting them reclaims nothing.

These tests pin the fix (object-level socket-sparing deletion at the RED
depth-1 sweep) and that the yellow/orange sweeps cannot touch a socket in
the cc-socks layout CC actually creates.

Harness idiom mirrors test_watchgod_loopbreak.py (deliberately duplicated —
repo precedent: the existing watchgod test files do not share a conftest):
HOME-redirected sandbox so the script's HOME-derived paths land in tmp_path,
overrides injected as a bash snippet after sourcing (the script assigns
CC_TMP_BUDGET_MB unconditionally, so env vars cannot set it), and a tmux
stub on a PATH-prepended bin dir so the RED tier's session-kill loop sees no
sessions.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# No-op-ish tmux stub: list-sessions prints nothing (no killable sessions),
# every other verb exits 0 — the RED kill loop is out of scope here.
_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

# Tier functions are called directly, so the budget only feeds orange's
# post-cleanup re-measure; keep it tiny. Stub queue_alert so the
# RED path's emergency alert becomes a log line instead of a real queue write.
_PRELUDE = (
    'CC_TMP_BUDGET_MB=4; queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
)


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"  # the script's default CC_TMP_DIR
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


def _mksock(path: Path) -> None:
    """A real socket-type inode via mknod — no AF_UNIX sun_path length limit,
    unprivileged on Linux, and exactly what `find -type s` matches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFSOCK | 0o600)


def _log(home: Path) -> str:
    return (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()


def test_red_preserves_socket_deletes_sibling_junk(tmp_path):
    """The incident replay: RED must delete reclaimable depth-1 dirs but never
    the control-plane socket. Pre-fix, rm -rf takes cc-socks with the rest."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "2501887.sock"
    _mksock(sock)
    (cctmp / "pyright-x").mkdir()
    (cctmp / "pyright-x" / "blob").write_bytes(b"x" * 4096)
    (cctmp / "gh-cli-cache").mkdir()
    (cctmp / "gh-cli-cache" / "f").write_bytes(b"y" * 4096)
    # a session dir, so newest_session resolves like production
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "RED deleted the control-plane socket"
    )
    assert not (cctmp / "pyright-x").exists(), "junk dir should be reclaimed"
    assert not (cctmp / "gh-cli-cache").exists(), "junk dir should be reclaimed"
    assert "preserved 1 unix socket" in _log(home)


def test_red_preserves_socket_with_no_session_dir(tmp_path):
    """The measured worst case: with NO claude-* session dir anywhere,
    newest_session is empty and the pre-fix guard spares NOTHING — every
    depth-1 dir including cc-socks is rm -rf'd. The socket must survive."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "999.sock"
    _mksock(sock)
    (cctmp / "junkdir").mkdir()
    (cctmp / "junkdir" / "f").write_bytes(b"z" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "RED deleted the socket in the empty-newest_session case"
    )
    assert (cctmp / "cc-socks").is_dir()


def test_red_reclaims_files_inside_socket_dir(tmp_path):
    """Object-level, not skip-wholesale: regular files sharing a directory
    with a socket ARE reclaimed; the socket and its parent dir survive."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "1234.sock"
    _mksock(sock)
    junk = cctmp / "cc-socks" / "junk.log"
    junk.write_bytes(b"j" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert not junk.exists(), "regular file beside the socket should be reclaimed"
    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode)
    assert (cctmp / "cc-socks").is_dir()


def test_orange_never_touches_old_socket_pin(tmp_path):
    """Pin: the yellow/orange sweeps are dir-name- and -type f-scoped and must
    never delete a socket, however old. Guards future sweep edits."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "999.sock"
    _mksock(sock)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(sock, (eight_days_ago, eight_days_ago))
    # give orange a cache to evict so the tier does real work
    (cctmp / "claude-skills").mkdir()
    (cctmp / "claude-skills" / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "yellow/orange sweep deleted a socket"
    )
    assert not (cctmp / "claude-skills").exists(), "orange should evict the cache"


# ── ORANGE's cache delete, routed through the socket-sparing primitive ───
#
# RED's cache sweep was routed through `reap_dir_sparing_sockets` after the
# 2026-09-05 severance, with a comment recording the measurement: of four
# sockets placed across the tree, the two inside cache directories were
# destroyed. ORANGE's identical sweep was left as `rm -rf`, which has no socket
# predicate — and ORANGE fires at 75% of budget while RED waits for 90%, so the
# unguarded copy is the one that actually runs.


def test_orange_does_not_destroy_a_socket_inside_a_cache_dir(tmp_path):
    """THE ACCEPTANCE BAR, replaying the reproduced defect.

    A live socket inside `claude-skills` is destroyed by this tier on the
    default branch. Sockets are 0 bytes, so sparing it reclaims exactly as much
    as deleting it did."""
    home, cctmp, bind = _sandbox(tmp_path)
    cache = cctmp / "claude-skills"
    cache.mkdir()
    _mksock(cache / "live.sock")
    (cache / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    sock = cache / "live.sock"
    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "ORANGE destroyed a live socket inside a cache directory"
    )
    # Negative control, in the same arm: sparing the socket must not turn the
    # sweep off. Without this, a guard that skipped the cache entirely passes.
    assert not (cache / "blob").exists(), "ORANGE stopped reclaiming the cache payload"


def test_orange_still_reclaims_a_cache_with_no_socket_in_it(tmp_path):
    """The other half of the control: routing through the primitive must not
    make an ordinary cache directory immortal. `reap_dir_sparing_sockets`
    removes a directory holding no sockets entirely, exactly like rm -rf."""
    home, cctmp, bind = _sandbox(tmp_path)
    for name in ("claude-skills", "tsx-abc"):
        d = cctmp / name
        d.mkdir()
        (d / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    for name in ("claude-skills", "tsx-abc"):
        assert not (cctmp / name).exists(), (
            f"a socket-free {name} should be reclaimed whole, as rm -rf did"
        )


def test_orange_spares_a_socket_in_a_NESTED_cache_dir(tmp_path):
    """The sweep matches by name at any depth, so the guard has to hold at
    depth too — a cache directory inside a session tree is the shape the RED
    comment's measurement actually used."""
    home, cctmp, bind = _sandbox(tmp_path)
    cache = cctmp / "claude-1000" / "proj" / "tsx-deep"
    cache.mkdir(parents=True)
    _mksock(cache / "deep.sock")
    (cache / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert (cache / "deep.sock").exists(), "ORANGE destroyed a socket in a nested cache"
    assert not (cache / "blob").exists(), "…and stopped reclaiming its payload"


def test_a_newline_in_a_cache_name_cannot_reap_outside_the_tree(tmp_path):
    """The reap loops are NUL-delimited, and this arm is why.

    `find` prints newline-terminated records, so a directory whose NAME
    contains a newline splits into two. The head is not a directory and the
    `[[ -d ]]` guard drops it — but the TAIL is a bare relative path, resolved
    against the daemon's working directory, which is the invoking user's home
    and not cc-tmp. MEASURED before the fix: `tsx-a<LF>b` yielded two records,
    the second a bare `b` that matched a real directory beside the daemon's
    cwd. Two failures in one: the cache is NOT reclaimed, and something outside
    the tree IS.

    Both halves are asserted: nothing outside the tree is touched, AND the
    newline-named directory itself is spared rather than reaped, because the
    liveness snapshot cannot represent it. The escape assertion alone would
    pass against a loop that deleted nothing at all."""
    home, cctmp, bind = _sandbox(tmp_path)

    # A directory beside the daemon's cwd, with the same basename as the tail
    # of the cache name. This is the thing that must not be touched.
    cwdbox = tmp_path / "cwdbox"
    (cwdbox / "b").mkdir(parents=True)
    victim = cwdbox / "b" / "victim.txt"
    victim.write_bytes(b"precious")

    cache = cctmp / "tsx-a\nb"
    cache.mkdir()
    (cache / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, f'cd "{cwdbox}" || exit 1\n' + _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert victim.exists(), (
        "the sweep followed a split record out of cc-tmp and deleted a "
        "directory beside the daemon's cwd"
    )
    # The cache SURVIVES, and deliberately so. NUL-delimiting makes the loop
    # REACH this directory, but `live_open_paths` renders /proc targets one per
    # line, so a descriptor held inside a newline-named directory appears
    # truncated and `dir_has_live_writer` reports no writer for a directory
    # that has one (MEASURED). Reaping it would mean deleting with the guard
    # silently answering the wrong question — worse than the accidental
    # survival the split record used to produce. So it is spared, and LOUDLY.
    assert cache.exists(), (
        "a newline-named directory was reaped even though the liveness guard "
        "cannot see writers inside it"
    )
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "cannot represent" in log, (
        "the directory was spared SILENTLY — the limitation has to be visible "
        f"or the next reader will call it a bug:\n{log}"
    )


def test_RED_spares_a_newline_named_dir_holding_a_LIVE_writer(tmp_path):
    """The shape the reviewer's example names, and the reason the sparing guard
    exists rather than just the NUL delimiter.

    MEASURED: `live_open_paths` renders /proc/*/fd targets one per LINE, so a
    descriptor held on `<cc-tmp>/pip-a<LF>b/part.whl` reaches the snapshot as
    two records, the first truncated to `<cc-tmp>/pip-a`. `dir_has_live_writer`
    searches those records for the directory prefix and finds nothing — it
    reports NO live writer for a directory that provably has one, while
    reporting correctly for a plain-named sibling.

    Before the loops were NUL-delimited this directory was unreachable and
    survived by accident. Reaching it without a snapshot that can represent it
    would have converted that accident into a deletion of a writer's work. The
    negative control is the plain-named sibling: it must still be reaped, or
    this arm would also pass against a sweep that stopped working."""
    home, cctmp, bind = _sandbox(tmp_path)
    nl_dir = cctmp / "pip-a\nb"
    nl_dir.mkdir()
    held = nl_dir / "part.whl"
    held.write_bytes(b"w" * 4096)
    junk = cctmp / "pip-plain"
    junk.mkdir()
    (junk / "blob").write_bytes(b"j" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    # ABOVE THE OXYGEN FLOOR, pinned explicitly.
    #
    # CORRECTION, MEASURED: an earlier draft of this comment said the 4MB
    # sandbox budget put the arm BELOW the floor. It does not. The floor reads
    # cc_tmp_headroom_mb = min(fs_total, CC_TMP_CAPACITY_MB default 2048) -
    # used, which is ~2048MB here; CC_TMP_BUDGET_MB feeds the TIER thresholds,
    # not the floor. Measured in this sandbox: capacity 2048, used 0, headroom
    # 2048, floor 150 — and clean_cc_red with no override logs no OXYGEN FLOOR
    # line. The override is kept for a different and real reason: a runner
    # whose tmp filesystem is smaller than 150MB would silently flip this arm
    # onto the emergency branch, where sparing is bypassed by design, and the
    # arm would then pass while testing the opposite contract.
    # A real held descriptor, exactly as the writer would have.
    with held.open("rb"):
        proc = _run(home, bind, _PRELUDE + "SACRED_GROUND_MB=1; clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert held.exists(), (
        "RED deleted a file a process was holding open, because the liveness "
        "snapshot could not represent its newline-named parent"
    )
    assert not junk.exists(), (
        "the plain-named directory survived too — the sweep did nothing, so "
        "the assertion above proves nothing"
    )


def _age(path: Path, seconds: int) -> None:
    """Backdate mtime/atime so a freshness predicate treats the file as stale."""
    old = time.time() - seconds
    os.utime(path, (old, old))


# --------------------------------------------------------------------------
# Sparing the directory-level reaper is NOT enough: the FILE sweeps walk the
# same tree. Review round 1 (Devin severe) named both — YELLOW's aged temp-file
# sweep and RED's loose-file sweep — and they carry no valid liveness exclusion
# for a newline-named path either, because they draw it from the same snapshot.
# The exclusion therefore lives in zone_a_live_exclusions, the one builder every
# Zone A deletion site consumes.
#
# The same round (Codex P2) named the other direction: below the oxygen floor
# the sparing must NOT apply, or a newline-named directory tree stays immortal
# while the volume sits at ENOSPC. Both arms are below.
# --------------------------------------------------------------------------


def test_YELLOW_temp_sweep_spares_an_aged_file_in_a_newline_named_dir(tmp_path):
    """A held `.tmp` ages past the 60-minute window while its writer still has
    it open. The freshness predicate stops protecting it, and the liveness
    exclusion never could — so without a newline exclusion this sweep unlinks
    in-flight work."""
    home, cctmp, bind = _sandbox(tmp_path)
    nl_dir = cctmp / "tsx-a\nb"
    nl_dir.mkdir()
    spared = nl_dir / "download.tmp"
    spared.write_bytes(b"w" * 2048)
    _age(spared, 7200)

    plain = cctmp / "tsx-plain"
    plain.mkdir()
    reaped = plain / "download.tmp"
    reaped.write_bytes(b"j" * 2048)
    _age(reaped, 7200)

    proc = _run(home, bind, _PRELUDE + "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert spared.exists(), (
        "YELLOW's temp sweep deleted a file inside a newline-named directory, "
        "which no liveness exclusion built from the snapshot can protect"
    )
    assert not reaped.exists(), (
        "the plain-named sibling survived too — the sweep did not run, so the "
        "assertion above proves nothing"
    )


def test_RED_loose_file_sweep_spares_a_file_in_a_newline_named_dir(tmp_path):
    """Above the floor, RED's whole-tree loose-file sweep must respect the same
    limitation the directory reaper does."""
    home, cctmp, bind = _sandbox(tmp_path)
    nl_dir = cctmp / "pip-a\nb"
    nl_dir.mkdir()
    spared = nl_dir / "part.whl"
    spared.write_bytes(b"w" * 4096)
    _age(spared, 600)

    plain = cctmp / "pip-plain"
    plain.mkdir()
    reaped = plain / "part.whl"
    reaped.write_bytes(b"j" * 4096)
    _age(reaped, 600)
    spared_file = cctmp / "spill-a\nb.log"
    spared_file.write_bytes(b"w" * 4096)
    _age(spared_file, 600)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    # ABOVE THE OXYGEN FLOOR, pinned explicitly.
    #
    # CORRECTION, MEASURED: an earlier draft of this comment said the 4MB
    # sandbox budget put the arm BELOW the floor. It does not. The floor reads
    # cc_tmp_headroom_mb = min(fs_total, CC_TMP_CAPACITY_MB default 2048) -
    # used, which is ~2048MB here; CC_TMP_BUDGET_MB feeds the TIER thresholds,
    # not the floor. Measured in this sandbox: capacity 2048, used 0, headroom
    # 2048, floor 150 — and clean_cc_red with no override logs no OXYGEN FLOOR
    # line. The override is kept for a different and real reason: a runner
    # whose tmp filesystem is smaller than 150MB would silently flip this arm
    # onto the emergency branch, where sparing is bypassed by design, and the
    # arm would then pass while testing the opposite contract.
    proc = _run(home, bind, _PRELUDE + "SACRED_GROUND_MB=1; clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert "OXYGEN FLOOR" not in _log(home), "the arm's premise: RED is above the floor"
    assert spared.exists(), "RED's loose-file sweep entered a newline-named dir"
    assert spared_file.exists(), (
        "RED's loose-file sweep deleted a newline-named file at depth 1 — the "
        "directory reaper never sees it, so only the builder's exclusion can "
        "protect it"
    )
    assert not reaped.exists(), (
        "the plain-named sibling survived too — the sweep did not run"
    )


def test_below_the_oxygen_floor_a_newline_named_tree_is_still_reclaimed(tmp_path):
    """The other direction, and the one that keeps the guard from becoming an
    immortality bug. Below the floor every discretionary exclusion is bypassed
    by design — an unverifiable writer loses to a certain ENOSPC — so the
    newline sparing must go with them."""
    home, cctmp, bind = _sandbox(tmp_path)
    nl_dir = cctmp / "pip-a\nb"
    nl_dir.mkdir()
    doomed = nl_dir / "part.whl"
    doomed.write_bytes(b"w" * 4096)
    # A newline-named FILE at depth 1, which the directory reaper cannot take
    # (it enumerates -type d) — so this one is reclaimable ONLY by the loose
    # sweep, and therefore ONLY if the builder actually drops its exclusion
    # below the floor. Without it this arm passes on the reaper alone and says
    # nothing about the gate it exists to pin.
    doomed_file = cctmp / "spill-a\nb.log"
    doomed_file.write_bytes(b"w" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    # SACRED_GROUND_MB is set after sourcing, so load_config's clamp does not
    # apply; a floor above any real headroom forces the emergency branch.
    proc = _run(home, bind, _PRELUDE + "SACRED_GROUND_MB=99999999; clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert "OXYGEN FLOOR" in _log(home), (
        "the arm's premise failed: RED did not take the floor branch\n" + _log(home)
    )
    assert not doomed.exists(), (
        "below the floor a newline-named tree was left immortal — it can hold "
        "the volume at ENOSPC while the daemon keeps killing sessions"
    )
    assert not doomed_file.exists(), (
        "below the floor the loose sweep still skipped a newline-named file; "
        "only the directory reaper reclaimed anything, so the exclusion is "
        "still installed in the emergency branch"
    )


def test_a_newline_in_the_ROOT_does_not_neuter_the_whole_sweep(tmp_path):
    """Audit finding, round 2. `-path`'s leading `*` matches `/`, so an
    unanchored `*<LF>*` exclusion is satisfied by the SEARCH ROOT's own path:
    with a newline anywhere in the root, every candidate matches the exclusion
    and Zone A stops reclaiming anything at all, silently. The same class bit
    an earlier PR in this arc one level up.

    Under such a root the liveness guard is degraded for the ENTIRE tree — no
    anchored pattern can separate representable paths from unrepresentable
    ones. The file's stance for a degraded in-flight guard is to proceed and
    say so, because refusing to reap lets cc-tmp fill, and a full cc-tmp is
    what kills sessions. So: the sweep still runs, and it is loud.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    root = home / ".genesis" / "cc-tmp-a\nb"
    root.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)

    aged = root / "stale.tmp"
    aged.write_bytes(b"j" * 2048)
    _age(aged, 7200)

    proc = _run(home, bind, _PRELUDE + f"CC_TMP_DIR={shlex.quote(str(root))}; clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert not aged.exists(), (
        "a newline in the ROOT matched the exclusion and neutered the entire "
        "sweep — cc-tmp would fill while the daemon reported a clean pass"
    )
    assert "DEGRADED for the ENTIRE tree" in _log(home), (
        "the sweep ran with no usable liveness guard and said nothing:\n"
        + _log(home)
    )


def test_the_spared_path_report_counts_names_not_descendants(tmp_path):
    """The 8-path cap is meant to bound the LOG. Without -prune every
    descendant of a newline-named directory also contains the newline, so a
    single bad name fills the cap and the operator is told that "more than 8
    paths" are affected when there is one."""
    home, cctmp, bind = _sandbox(tmp_path)
    nl_dir = cctmp / "tsx-a\nb"
    nl_dir.mkdir()
    for i in range(20):
        (nl_dir / f"f{i}").write_bytes(b"x")

    proc = _run(home, bind, _PRELUDE + "clean_cc_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    log = _log(home)
    assert log.count("its name contains a newline") == 1, (
        "the report listed descendants instead of offending names:\n" + log
    )
    assert "more than 8 paths" not in log, (
        "one bad name tripped the cap meant for eight of them:\n" + log
    )
