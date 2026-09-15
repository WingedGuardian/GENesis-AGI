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
