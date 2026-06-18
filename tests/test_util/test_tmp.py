"""Tests for genesis.util.tmp.big_tmp_dir — the dedicated large-temp directory.

Large runtime producers (yt-dlp audio, STT uploads, git worktrees, eval artifacts)
must keep their temp OFF ~/.genesis/cc-tmp (the watchgod-policed 'oxygen' folder) by
passing dir=big_tmp_dir(). These verify the helper resolves + creates the right dir
and that tempfile actually honors it.
"""

import tempfile
from pathlib import Path

from genesis.util.tmp import big_tmp_dir


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
