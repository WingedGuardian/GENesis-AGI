"""Tests for genesis.util.tmp.big_tmp_dir — the dedicated large-temp directory.

Large runtime producers (yt-dlp audio, STT uploads, git worktrees, eval artifacts)
must keep their temp OFF ~/.genesis/cc-tmp (the watchgod-policed 'oxygen' folder) by
passing dir=big_tmp_dir(). These verify the helper resolves + creates the right dir
and that tempfile actually honors it.
"""

import os
import tempfile
from pathlib import Path

from genesis.util.tmp import big_tmp_dir, should_redirect_pytest_basetemp


def test_default_is_home_tmp_and_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENESIS_BIG_TMP", raising=False)
    d = big_tmp_dir()
    assert d == str(tmp_path / "tmp")
    assert Path(d).is_dir(), "big_tmp_dir must create the directory"


def test_honors_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom-big-tmp"
    monkeypatch.setenv("GENESIS_BIG_TMP", str(override))
    d = big_tmp_dir()
    assert d == str(override)
    assert Path(d).is_dir()


def test_tempfile_lands_under_big_tmp_dir(tmp_path, monkeypatch):
    """A NamedTemporaryFile created with dir=big_tmp_dir() lives under it — the exact
    mechanism the runtime large-producers use to keep temp off cc-tmp."""
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "big"))
    d = big_tmp_dir()
    with tempfile.NamedTemporaryFile(dir=d, suffix=".x") as f:
        assert Path(f.name).parent == Path(d)


# ── should_redirect_pytest_basetemp — the (pure, side-effect-free) decision ──

_HOME = "/home/genesisuser"
_CCTMP = "/home/genesisuser/.genesis/cc-tmp"


def test_redirect_when_tmpdir_is_cc_tmp():
    """The core case: TMPDIR == cc-tmp and no --basetemp → redirect."""
    assert should_redirect_pytest_basetemp(None, _CCTMP, _HOME) is True


def test_redirect_when_tmpdir_under_cc_tmp():
    """A subdir of cc-tmp also redirects (prefix match, not just exact)."""
    assert should_redirect_pytest_basetemp(None, _CCTMP + "/sub", _HOME) is True


def test_noredirect_when_explicit_basetemp_passed():
    """An explicit --basetemp always wins — never override the caller."""
    assert should_redirect_pytest_basetemp("/some/where", _CCTMP, _HOME) is False


def test_noredirect_on_ci_tmpdir_unset():
    """CI leaves TMPDIR unset → no-op (CI keeps its own default tmp)."""
    assert should_redirect_pytest_basetemp(None, None, _HOME) is False
    assert should_redirect_pytest_basetemp(None, "", _HOME) is False


def test_noredirect_when_tmpdir_elsewhere():
    """A TMPDIR that isn't cc-tmp (e.g. /tmp) is left alone."""
    assert should_redirect_pytest_basetemp(None, "/tmp", _HOME) is False


def test_redirect_prefix_is_boundary_safe():
    """A sibling dir sharing the cc-tmp name PREFIX must NOT match (path-boundary)."""
    assert should_redirect_pytest_basetemp(None, _CCTMP + "-evil", _HOME) is False


def test_redirect_resolves_symlinked_tmpdir_into_cc_tmp(tmp_path):
    """A TMPDIR symlink that resolves INTO cc-tmp still redirects (realpath)."""
    home = tmp_path
    cc = home / ".genesis" / "cc-tmp"
    cc.mkdir(parents=True)
    link = tmp_path / "tmpdir-link"
    link.symlink_to(cc)
    assert should_redirect_pytest_basetemp(None, str(link), str(home)) is True


def test_predicate_is_pure_no_filesystem_side_effect(tmp_path, monkeypatch):
    """The predicate must NOT create ~/tmp (or anything) — that side-effect on the
    no-op path was the regression this refactor fixes."""
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "should-not-exist"))
    should_redirect_pytest_basetemp(None, None, str(tmp_path))  # CI no-op path
    assert not (tmp_path / "should-not-exist").exists()


def test_pytest_unconfigure_removes_per_pid_basetemp(tmp_path):
    """The conftest's pytest_unconfigure deletes the per-pid basetemp it recorded,
    so ~/tmp/pytest/<pid> dirs can't accumulate (the retention-leak fix). Tests the
    real conftest functions loaded fresh."""
    import importlib.util
    from types import SimpleNamespace

    conftest_path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_genesis_conftest_probe", conftest_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    leaf = tmp_path / "pytest" / "12345"
    leaf.mkdir(parents=True)
    (leaf / "scratch.txt").write_text("x")
    mod.pytest_unconfigure(SimpleNamespace(_genesis_basetemp_cleanup=str(leaf)))
    assert not leaf.exists(), "per-pid basetemp not cleaned at unconfigure"
    # No-op safe when nothing was recorded (e.g. the redirect never fired).
    mod.pytest_unconfigure(SimpleNamespace())


def test_reap_stale_pytest_basetemps(tmp_path):
    """The startup sweep reaps per-PID leaves from abnormal exits (dead PID),
    spares live runs (this process), and ignores non-PID / pid<=1 names — so a
    SIGKILL/crash can't leak the basetemp (pytest_unconfigure never runs then)."""
    import importlib.util
    import subprocess

    conftest_path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_genesis_conftest_probe2", conftest_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    base = tmp_path / "pytest"
    base.mkdir()
    # A genuinely-dead PID: spawn + reap a trivial process, then reuse its PID.
    proc = subprocess.Popen(["true"])
    proc.wait()
    dead = base / str(proc.pid)
    dead.mkdir()
    (dead / "scratch").write_text("x")
    live = base / str(os.getpid())
    live.mkdir()
    non_pid = base / "not-a-pid"
    non_pid.mkdir()
    init = base / "1"
    init.mkdir()
    # Unexpected on-disk state the reaper must survive without raising (it would
    # otherwise break collection for the WHOLE suite):
    superscript = base / "²"  # '²'.isdigit() True but int('²') → ValueError
    superscript.mkdir()
    huge = base / ("9" * 40)  # os.kill(10**40, 0) → OverflowError (not an OSError)
    huge.mkdir()

    mod._reap_stale_pytest_basetemps(str(base))  # must NOT raise

    assert not dead.exists(), "dead-PID leaf must be reaped"
    assert live.exists(), "live-PID (this process) must be spared"
    assert non_pid.exists(), "non-PID name must be ignored"
    assert init.exists(), "pid<=1 must be skipped (never signal 0/1)"
    assert superscript.exists(), "unicode-digit name must be skipped, not crash"
    assert huge.exists(), "out-of-range PID name must be spared, not crash"
    # No crash on a missing base dir.
    mod._reap_stale_pytest_basetemps(str(tmp_path / "does-not-exist"))
