"""Tests for the mcp-spawn retention prune in scripts/disk_hygiene.sh
(``prune_mcp_spawn``).

Sources the script (functions only — ``main`` is guarded) and calls
``prune_mcp_spawn`` against a fixture dir. A slot file is kept iff its recorded
session pid is still alive; dead-pid, malformed, and stale temp files are pruned.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _run(spawn_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_mcp_spawn '{spawn_dir}'"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _dead_pid() -> int:
    """A pid guaranteed dead: spawn `true`, reap it, return its (freed) pid."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_live_pid_file_kept(tmp_path):
    d = tmp_path / "mcp-spawn"
    d.mkdir()
    f = d / "3"
    f.write_text(f"{os.getpid()} 0123456789abcdef\n")  # our own live pid
    _run(d)
    assert f.exists()


def test_dead_pid_file_pruned(tmp_path):
    d = tmp_path / "mcp-spawn"
    d.mkdir()
    f = d / "4"
    f.write_text(f"{_dead_pid()} deadc0de\n")
    _run(d)
    assert not f.exists()


def test_malformed_file_pruned(tmp_path):
    d = tmp_path / "mcp-spawn"
    d.mkdir()
    f = d / "5"
    f.write_text("no-pid-here\n")
    _run(d)
    assert not f.exists()


def test_stale_temp_file_pruned(tmp_path):
    d = tmp_path / "mcp-spawn"
    d.mkdir()
    tmp = d / ".5.abcd"  # leftover atomic-write temp
    tmp.write_text("whatever")
    old = time.time() - 2 * 3600
    os.utime(tmp, (old, old))
    _run(d)
    assert not tmp.exists()


def test_missing_dir_is_noop(tmp_path):
    r = _run(tmp_path / "nope")
    assert r.returncode == 0
