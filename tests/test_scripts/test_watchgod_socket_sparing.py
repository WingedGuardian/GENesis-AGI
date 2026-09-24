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
    'CC_TMP_BUDGET_MB=4; '
    'queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
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


def test_red_preserves_a_session_that_has_not_written_a_file_yet(tmp_path):
    """A session between creating its workspace and writing its first file.

    Selecting on `-type f` alone found no candidate there, so nothing was
    preserved and the depth-1 sweep reaped the live session's own directories out
    from under it — its next write gets ENOENT. A brand-new session IS a fresh
    directory and nothing else, so directories have to count as evidence of life.
    (Codex P2 on #1856; the regression was introduced by the newest-FILE fix in
    the commit before it, which is why both shapes are pinned here.)

    The fixture is deliberately the ONLY session, with no file anywhere under a
    `claude-*` tree: that is what leaves a `-type f` search with no candidate at
    all, and an empty selection means the depth-1 skip protects NOTHING. With any
    other file present the skip covers the whole `claude-<uid>` tree and the
    hazard is masked — an earlier draft of this test made exactly that mistake
    and passed against the unfixed code.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    starting = cctmp / "claude-1000" / "-home-dev-starting" / "session-new"
    starting.mkdir(parents=True)  # dirs exist, no file written yet
    canary = cctmp / "reclaimable-junk"  # outside claude-*, so it cannot be selected
    canary.mkdir()
    (canary / "blob").write_bytes(b"x" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert starting.is_dir(), (
        "RED reaped a session that had created its workspace but not yet written "
        "a file — the live process's next write would fail with ENOENT"
    )
    assert not (canary / "blob").exists(), (
        "the canary survived, so RED reclaimed nothing and the assertion above "
        "proved nothing"
    )




def test_red_does_not_let_a_claude_named_decoy_outside_the_tree_win(tmp_path):
    """RED's preserve-selector must never resolve to a directory outside the
    ``claude-<uid>/`` tree, however new an entry inside it happens to be.

    This is the anti-regression for the defect two attempts at "improve the
    selector" shipped (PR #1856): ``find``'s ``-path`` matches the WHOLE path
    and its ``*`` crosses ``/``, so widening the selector's depth turns
    ``-path "*/claude-*"`` into "a component OR BASENAME beginning ``claude-``
    anywhere". A file under an unrelated depth-1 directory then wins the mtime
    sort, the winner is reduced to the wrong project root, and the depth-1 loop
    reaps the real live workspace — while the log still says "preserving active
    session".

    The shape is not hypothetical: pytest basetemps live inside cc-tmp on this
    install, so the watchgod suite itself plants ``claude-*`` fixtures in the
    directory the daemon sweeps.
    """
    home, cctmp, bind = _sandbox(tmp_path)

    live = cctmp / "claude-1000" / "-home-dev-active" / "session-a"
    live.mkdir(parents=True)
    (live / "live.json").write_bytes(b"L" * 4096)

    # The decoy: basename begins "claude-", depth 3, under a NON-claude depth-1
    # dir, and strictly the newest thing in the tree.
    decoy = cctmp / "tmpXYZ" / "sub"
    decoy.mkdir(parents=True)
    (decoy / "claude-notes.txt").write_bytes(b"D" * 4096)
    old = time.time() - 600
    os.utime(live / "live.json", (old, old))
    os.utime(live, (old, old))

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert (live / "live.json").exists(), (
        "RED deleted the live session workspace: a claude-named decoy OUTSIDE "
        "the claude-<uid> tree won the preserve-selector"
    )
    # Guard the guard: if the decoy survived too, RED preserved everything and
    # the assertion above is vacuous.
    assert not (decoy / "claude-notes.txt").exists(), (
        "the decoy survived as well, so RED reclaimed nothing and this test "
        "proves nothing"
    )


def test_red_spares_an_empty_socket_dir_but_still_reclaims_files_in_it(tmp_path):
    """cc-socks must survive RED even with no socket inside it — it is the
    directory the next session binds into — while non-socket files sitting in
    it are still reclaimable.

    Sparing socket INODES is not enough: an empty cc-socks has nothing to keep
    it non-empty, so the depth-first pass removes the directory itself. Zone B's
    empty-dir sweep already spares it; this pins the same rule in Zone A.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    socks = cctmp / "cc-socks"
    socks.mkdir()
    junk = socks / "stale.log"
    junk.write_bytes(b"j" * 8192)
    old = time.time() - 600
    os.utime(junk, (old, old))

    session = cctmp / "claude-1000" / "-home-dev-active" / "session-a"
    session.mkdir(parents=True)
    (session / "live.json").write_bytes(b"L" * 1024)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert socks.is_dir(), (
        "RED removed an EMPTY cc-socks — the next session has nowhere to bind"
    )
    assert not junk.exists(), (
        "the reclaimable file inside cc-socks survived, so the directory "
        "exclusion widened to its contents"
    )
