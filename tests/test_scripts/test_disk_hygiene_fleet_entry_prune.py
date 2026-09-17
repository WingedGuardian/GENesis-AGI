"""Tests for the fleet entry-capture retention prune in scripts/disk_hygiene.sh.

Mirrors test_disk_hygiene_guard_corpus_prune's source-and-call pattern: the
script only DEFINES functions when sourced (``main`` is guarded), so the prune
can be exercised alone. Age is set via os.utime, so these are wall-clock
independent.

``scripts/fleet_entry_capture.sh`` appends one dated file per UTC day, forever.
The store is small but unbounded, and an unbounded store on a smaller disk than
this one is a slow leak whatever its rate — so the retention ships with the
writer rather than as a follow-up.

The preservation case that matters most is the SIBLING one: these logs live in
``~/.genesis/logs`` alongside ``cc_exit_*.log``, which has no retention of its
own and must not be collected by a neighbour's glob.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _run_prune(log_dir: Path) -> subprocess.CompletedProcess:
    # Paths go in as bash POSITIONAL PARAMETERS, never interpolated into script
    # text: either can contain a quote, which would otherwise end the string and
    # turn the rest of the path into shell source.
    #
    # check=True catches a failed `source` or a renamed function. It does NOT
    # catch a failing `find`: the function ends `find … -delete 2>/dev/null ||
    # echo "fleet-entry prune exited $?"`, so a find that fails is swallowed and
    # the function still exits 0. Every preservation assertion below would also
    # pass against a prune that never ran, so they additionally assert the
    # absence of that "exited" line.
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\nprune_fleet_entry_logs "$2"',
            "prune_fleet_entry_logs",
            str(_HYGIENE),
            str(log_dir),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def _assert_ran_clean(proc: subprocess.CompletedProcess) -> None:
    assert "fleet-entry prune exited" not in proc.stdout, proc.stdout
    assert "fleet-entry prune exited" not in proc.stderr, proc.stderr


def test_an_aged_log_is_pruned(tmp_path):
    old = tmp_path / "fleet_entry_2026-01-01.log"
    old.write_text("=== entry=lobby\n")
    _age(old, 60)
    _assert_ran_clean(_run_prune(tmp_path))
    assert not old.exists()


def test_a_recent_log_is_kept(tmp_path):
    recent = tmp_path / "fleet_entry_2026-09-16.log"
    recent.write_text("=== entry=lobby\n")
    _age(recent, 10)
    _assert_ran_clean(_run_prune(tmp_path))
    assert recent.exists()


def test_a_sibling_log_in_the_same_dir_is_never_collected(tmp_path):
    """cc_exit_*.log shares ~/.genesis/logs and has NO retention of its own.

    A glob written as ``*.log`` — or even ``fleet*`` — would silently start
    deleting another subsystem's diagnostics, and nothing would report it.
    """
    sibling = tmp_path / "cc_exit_4.log"
    sibling.write_text("exit=0\n")
    _age(sibling, 400)
    other = tmp_path / "cc_tmp_top_20260730T175449Z.txt"
    other.write_text("x\n")
    _age(other, 400)
    _assert_ran_clean(_run_prune(tmp_path))
    assert sibling.exists(), "the prune ate a sibling subsystem's log"
    assert other.exists()


def test_a_missing_log_dir_is_not_an_error(tmp_path):
    """A fresh install has never connected, so the dir may not exist yet."""
    _assert_ran_clean(_run_prune(tmp_path / "does-not-exist"))


def test_a_subdirectory_is_not_descended_into(tmp_path):
    """maxdepth 1: the prune owns its own dated files, not a tree."""
    sub = tmp_path / "nested"
    sub.mkdir()
    buried = sub / "fleet_entry_2026-01-01.log"
    buried.write_text("x\n")
    _age(buried, 400)
    _assert_ran_clean(_run_prune(tmp_path))
    assert buried.exists()


def test_the_groom_actually_invokes_the_pruner(tmp_path):
    """Wiring, not existence. A prune function nothing calls is retention that
    never runs, and every test above would still pass."""
    text = _HYGIENE.read_text()
    assert 'prune_fleet_entry_logs "$HOME/.genesis/logs"' in text, (
        "prune_fleet_entry_logs is defined but main() never calls it"
    )
