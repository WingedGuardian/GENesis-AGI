"""Tests for the guard-corpus retention prune in scripts/disk_hygiene.sh.

Sources the script (which only DEFINES functions when sourced — ``main`` is
guarded) and calls ``prune_guard_corpus`` against a fixture dir, mirroring
test_disk_hygiene_tmp_prune's source-and-call pattern. Age is set via os.utime,
so these are wall-clock-independent.

The corpus cache holds verbatim command lines from real sessions, including
secrets passed in argv, and after a measurement nothing reads it. It is also
fully regenerable, so ageing it out costs a rebuild and nothing else.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _run_prune(out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_guard_corpus '{out_dir}'"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_an_aged_corpus_is_pruned(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    corpus = out / "guard-corpus.jsonl"
    corpus.write_text('["echo hi", "/tmp"]\n')
    _age(corpus, 60)

    _run_prune(out)

    assert not corpus.exists()


def test_a_recent_corpus_is_kept(tmp_path):
    """A measurement in progress must not lose its cache mid-run."""
    out = tmp_path / "output"
    out.mkdir()
    corpus = out / "guard-corpus.jsonl"
    corpus.write_text('["echo hi", "/tmp"]\n')
    _age(corpus, 3)

    _run_prune(out)

    assert corpus.exists()


def test_an_orphaned_rebuild_temp_is_pruned(tmp_path):
    """The rebuild writes through mkstemp and unlinks its own temp on failure,
    but a SIGKILL mid-write leaves one behind — holding the same command lines
    with none of the value."""
    out = tmp_path / "output"
    out.mkdir()
    temp = out / "guard-corpus.jsonl.a1b2c3.tmp"
    temp.write_text('["echo interrupted", "/tmp"]\n')
    _age(temp, 60)

    _run_prune(out)

    assert not temp.exists()


def test_unrelated_files_in_the_output_dir_are_untouched(tmp_path):
    """The prune is NAME-scoped, deliberately.

    ~/.genesis/output is a shared directory outside the repo that other
    subsystems write into. A prune that aged out everything in it would be a far
    worse hazard than the file it exists to remove, and the failure would be
    silent and total.
    """
    out = tmp_path / "output"
    out.mkdir()
    others = [out / "some_report.md", out / "another_export.jsonl", out / "guard-corpus.txt"]
    for f in others:
        f.write_text("keep me\n")
        _age(f, 400)

    _run_prune(out)

    for f in others:
        assert f.exists(), f"the prune deleted an unrelated file: {f.name}"


def test_a_missing_output_dir_is_a_noop(tmp_path):
    """A fresh install has never run a measurement, so the directory may not
    exist. The groom must not report an error for that."""
    result = _run_prune(tmp_path / "does-not-exist")

    assert result.returncode == 0
    assert "exited" not in result.stdout
