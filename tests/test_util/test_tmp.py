"""Tests for genesis.util.tmp.big_tmp_dir — the dedicated large-temp directory.

Large runtime producers (yt-dlp audio, STT uploads, git worktrees, eval artifacts)
must keep their temp OFF ~/.genesis/cc-tmp (the watchgod-policed 'oxygen' folder) by
passing dir=big_tmp_dir(). These verify the helper resolves + creates the right dir
and that tempfile actually honors it.
"""

import tempfile
from pathlib import Path

from genesis.util.tmp import big_tmp_dir, pytest_basetemp_override


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


# ── pytest_basetemp_override — the decision that keeps pytest tmp off cc-tmp ──

_HOME = "/home/genesisuser"
_BIG = "/home/genesisuser/tmp"
_CCTMP = "/home/genesisuser/.genesis/cc-tmp"


def test_override_redirects_when_tmpdir_is_cc_tmp():
    """The core case: TMPDIR == cc-tmp and no --basetemp → redirect under big_tmp."""
    got = pytest_basetemp_override(None, _CCTMP, _HOME, _BIG)
    assert got == "/home/genesisuser/tmp/pytest"


def test_override_redirects_when_tmpdir_under_cc_tmp():
    """A subdir of cc-tmp also redirects (prefix match, not just exact)."""
    got = pytest_basetemp_override(None, _CCTMP + "/sub", _HOME, _BIG)
    assert got == "/home/genesisuser/tmp/pytest"


def test_override_noop_when_explicit_basetemp_passed():
    """An explicit --basetemp always wins — never override the caller."""
    assert pytest_basetemp_override("/some/where", _CCTMP, _HOME, _BIG) is None


def test_override_noop_on_ci_tmpdir_unset():
    """CI leaves TMPDIR unset → no-op (CI keeps its own default tmp)."""
    assert pytest_basetemp_override(None, None, _HOME, _BIG) is None
    assert pytest_basetemp_override(None, "", _HOME, _BIG) is None


def test_override_noop_when_tmpdir_elsewhere():
    """A TMPDIR that isn't cc-tmp (e.g. /tmp) is left alone."""
    assert pytest_basetemp_override(None, "/tmp", _HOME, _BIG) is None


def test_override_prefix_is_boundary_safe():
    """A sibling dir sharing the cc-tmp name PREFIX must NOT match (path-boundary)."""
    assert pytest_basetemp_override(None, _CCTMP + "-evil", _HOME, _BIG) is None


def test_override_resolves_symlinked_tmpdir_into_cc_tmp(tmp_path):
    """A TMPDIR symlink that resolves INTO cc-tmp still redirects (realpath)."""
    home = tmp_path
    cc = home / ".genesis" / "cc-tmp"
    cc.mkdir(parents=True)
    link = tmp_path / "tmpdir-link"
    link.symlink_to(cc)
    got = pytest_basetemp_override(None, str(link), str(home), str(home / "tmp"))
    assert got == str(home / "tmp" / "pytest")
