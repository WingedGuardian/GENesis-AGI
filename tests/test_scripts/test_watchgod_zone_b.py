"""tmp_watchgod Zone B (/tmp housekeeping) — it must not delete a control plane either.

Zone B had NO test coverage before 2026-09-07, because its sweeps named `/tmp`
literally and a test would have had to sweep the real one. That gap is how the
finding below survived an audit: an earlier pass over this daemon marked the
empty-directory sweep SAFE on the reasoning that "socket-holding dirs are
non-empty" — true of the directory holding the socket, and false of its siblings.

MEASURED on a live install, running the pre-fix predicate with `-print` instead of
`-delete`: it matched `/tmp/cc-socks` and `/tmp/cc-daemon-1000/<id>/pty`. The
second is an empty rendezvous directory inside a LIVE Claude Code daemon's socket
tree — created up front and populated later — held by a process with 11 days of
uptime. Deleting it is the same failure class as the 2026-09-05 incident: the
socket itself is spared by name, and the directory it needs is not.

Zone B is currently unreachable on that install (`/tmp` is not tmpfs there, so the
tier comes from absolute free space, which was ~13 GB) — latent, not firing.
Latent is worth fixing and worth pinning: free space falls.

The sweeps now run against `$SYS_TMP_DIR`, which exists solely so these tests can
point the zone at a sandbox. Nothing in production sets it.

TWO THINGS KEEP THESE TESTS FROM BEING VACUOUS, and the first was learned the hard
way — an earlier draft of this file passed for the wrong reason.

  1. The swept sandbox is NOT `tmp_path`. Zone B carries a pre-existing
     `-not -path "*/pytest-*"` exclusion, and pytest's basetemp is
     `…/pytest-of-<user>/pytest-<n>/…` under some configurations — so every
     survival assertion under `tmp_path` would pass because the sweep skipped the
     whole tree, on a path that varies between runners. MEASURED: the same four
     tests deleted correctly under one basetemp and swept nothing under another.
  2. Every survival test plants a CANARY — an ordinary empty directory the sweep
     MUST reclaim. If the canary survives, the sweep did not run and the survival
     assertion proved nothing, so the test fails instead of passing quietly.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = "#!/usr/bin/env bash\nexit 0\n"

_DAY = 86400


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _age(path: Path, days: float) -> None:
    when = time.time() - days * _DAY
    os.utime(path, (when, when))


@pytest.fixture
def zone_b(tmp_path):
    """HOME under tmp_path (never swept), but the SWEPT tree somewhere neutral —
    see the module docstring on why `tmp_path` cannot be the swept directory."""
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    systmp = Path(tempfile.mkdtemp(prefix="wgzoneb"))
    try:
        yield home, systmp, bind
    finally:
        shutil.rmtree(systmp, ignore_errors=True)


def _run(home: Path, systmp: Path, bind: Path, snippet: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}", SYS_TMP_DIR=str(systmp))
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _canary(systmp: Path) -> Path:
    """An ordinary empty dir the sweep MUST reclaim. If it survives, the sweep did
    not run and every survival assertion beside it is meaningless."""
    c = systmp / "canary-ordinary-empty-dir"
    c.mkdir()
    _age(c, 30)
    return c


def _assert_swept(canary: Path) -> None:
    assert not canary.exists(), (
        "the canary survived, so the sweep never ran over this tree — any survival "
        "assertion in this test would have passed for the wrong reason"
    )


def test_empty_dir_inside_a_live_socket_tree_survives(zone_b):
    """The measured shape: CC's daemon tree, with an empty rendezvous directory
    beside the sockets. The sockets are spared by name; the directory they need
    has to be spared too, or the tree is broken just as thoroughly."""
    home, systmp, bind = zone_b
    canary = _canary(systmp)
    tree = systmp / "cc-daemon-1000" / "5c860797"
    (tree / "spare").mkdir(parents=True)
    os.mknod(tree / "control.sock", stat.S_IFSOCK | 0o600)
    os.mknod(tree / "spare" / "c3436363.claim.sock", stat.S_IFSOCK | 0o600)
    pty = tree / "pty"  # empty, created up front, populated later
    pty.mkdir()
    _age(pty, 30)

    proc = _run(home, systmp, bind, "clean_sys_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_swept(canary)
    assert pty.is_dir(), "an empty rendezvous dir inside a live CC daemon tree was deleted"
    assert (tree / "control.sock").exists(), "a daemon socket was deleted"


def test_empty_cc_socks_dir_survives(zone_b):
    """`cc-socks` is where the per-session messaging sockets land on an install
    whose temp dir is /tmp. Empty right now is not a reason to remove it: it is
    the directory the next session binds into."""
    home, systmp, bind = zone_b
    canary = _canary(systmp)
    socks = systmp / "cc-socks"
    socks.mkdir()
    _age(socks, 30)

    proc = _run(home, systmp, bind, "clean_sys_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _assert_swept(canary)
    assert socks.is_dir(), "the socket directory was deleted while empty"


def test_ordinary_empty_dirs_are_still_reclaimed(zone_b):
    """The sweep still does its job — the exclusions are narrow, not a disable."""
    home, systmp, bind = zone_b
    junk = systmp / "leftover-build-dir"
    junk.mkdir()
    _age(junk, 30)

    proc = _run(home, systmp, bind, "clean_sys_yellow")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not junk.exists(), "an ordinary empty dir should still be reclaimed"


def test_socket_files_survive_every_zone_b_tier(zone_b):
    """The pre-existing `-not -name '*.sock'` exclusion, pinned across all three
    tiers — RED is the aggressive one and the easiest to regress."""
    home, systmp, bind = zone_b
    sock = systmp / "cc-socks" / "42.sock"
    sock.parent.mkdir()
    os.mknod(sock, stat.S_IFSOCK | 0o600)
    junk = systmp / "old.log"
    junk.write_text("x")
    for p in (sock, junk, sock.parent):
        _age(p, 30)

    for tier in ("clean_sys_yellow", "clean_sys_orange", "clean_sys_red"):
        proc = _run(home, systmp, bind, tier)
        assert proc.returncode == 0, f"{tier}: {proc.stdout}\n{proc.stderr}"
        assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
            f"{tier} deleted a unix socket under the swept tree"
        )
    assert not junk.exists(), "the sweeps must still reclaim ordinary old files"
