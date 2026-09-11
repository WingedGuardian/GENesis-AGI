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
    # Both paths are passed as bash POSITIONAL PARAMETERS, never interpolated
    # into the script text. _HYGIENE comes from the checkout path and out_dir
    # from pytest's tmp_path; either can contain a single quote, which would
    # break the f-string's quoting and turn the rest of the path into shell
    # source. Positional parameters keep them data.
    #
    # check=True is load-bearing rather than tidiness: every PRESERVATION test
    # below asserts that a file still EXISTS, which is exactly what a prune that
    # never ran also produces. Without it those tests pass on a broken script —
    # the vacuous shape where the assertion is equally true on the failure path.
    #
    # It is NOT sufficient on its own, and an earlier version of this comment
    # claimed it was. `prune_guard_corpus` ends
    # `find … -delete 2>/dev/null || echo "guard-corpus prune exited $?"`, so a
    # find that FAILS is swallowed by the `||` and the function still exits 0 —
    # invisible to check=True. What check=True actually catches is a failed
    # `source` or a renamed function. The preservation tests therefore also
    # assert the absence of that "exited" line; see _assert_ran_clean.
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\nprune_guard_corpus "$2"',
            "prune_guard_corpus",
            str(_HYGIENE),
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def _assert_ran_clean(result: subprocess.CompletedProcess) -> None:
    """The half check=True cannot see.

    `prune_guard_corpus` swallows a failing `find` with
    `|| echo "guard-corpus prune exited $?"` and still exits 0, so a preservation
    test — which only asserts a file still EXISTS — passes identically whether the
    prune ran and correctly kept the file, or errored and never looked at it.
    That echoed line is the only signal, so every test asserts its absence.
    """
    assert result.returncode == 0, f"prune exited {result.returncode}: {result.stderr}"
    assert "exited" not in result.stdout, (
        f"the prune reported an internal failure and was swallowed: {result.stdout!r}"
    )


def test_an_aged_corpus_is_pruned(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    corpus = out / "guard-corpus.jsonl"
    corpus.write_text('["echo hi", "/tmp"]\n')
    _age(corpus, 60)

    _assert_ran_clean(_run_prune(out))

    assert not corpus.exists()


def test_a_recent_corpus_is_kept(tmp_path):
    """A measurement in progress must not lose its cache mid-run."""
    out = tmp_path / "output"
    out.mkdir()
    corpus = out / "guard-corpus.jsonl"
    corpus.write_text('["echo hi", "/tmp"]\n')
    _age(corpus, 3)

    _assert_ran_clean(_run_prune(out))

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

    _assert_ran_clean(_run_prune(out))

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

    _assert_ran_clean(_run_prune(out))

    for f in others:
        assert f.exists(), f"the prune deleted an unrelated file: {f.name}"


def test_a_missing_output_dir_is_a_noop(tmp_path):
    """A fresh install has never run a measurement, so the directory may not
    exist. The groom must not report an error for that."""
    _assert_ran_clean(_run_prune(tmp_path / "does-not-exist"))
