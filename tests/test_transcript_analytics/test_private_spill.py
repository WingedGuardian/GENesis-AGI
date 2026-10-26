"""Actual DuckDB spill storage stays private under permissive caller settings."""

import gc
import os
import stat
from pathlib import Path

import pytest

from genesis.transcript_analytics import query


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("preexisting", [False, True])
def test_spill_uses_private_connection_children(tmp_path, monkeypatch, override, preexisting):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ("custom" if override else "tmp/transcript-analytics/duckdb")
    if override:
        monkeypatch.setenv("TA_DUCKDB_TMP", str(root))
    else:
        monkeypatch.delenv("TA_DUCKDB_TMP", raising=False)
    if preexisting:
        root.mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    old = os.umask(0o022)
    first = second = None
    try:
        first = query.connect(data, live=True, memory_limit="16MB", threads=1)
        second = query.connect(data, live=True, memory_limit="16MB", threads=1)
        children = sorted(root.glob("private-*"))
        assert len(children) == 2
        for child in children:
            assert stat.S_IMODE(child.stat().st_mode) == 0o700
            assert child.stat().st_uid == os.getuid()
        first.execute("CREATE TEMP TABLE spill AS SELECT i, md5(i::VARCHAR) s FROM range(400000) t(i)")
        spilled = [p for child in children for p in child.rglob("*") if p.is_file()]
        assert spilled, "the privacy test must actually spill"
        assert all(p.parent in children for p in spilled)
        assert second.execute("SELECT count(*) FROM turns").fetchone() == (0,)
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        first = second = None
        gc.collect()
        os.umask(old)
    assert not list(root.glob("private-*"))
    if preexisting:
        assert stat.S_IMODE(root.stat().st_mode) == 0o755


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_unsafe_spill_roots_refuse_before_connection(tmp_path, monkeypatch, kind):
    root = tmp_path / "root"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        root.write_text("keep")
    else:
        root.mkdir()
        root.chmod(0o777)
    monkeypatch.setenv("TA_DUCKDB_TMP", str(root))
    with pytest.raises((ValueError, OSError)):
        query.connect(tmp_path / "data", live=True)
    assert not list(tmp_path.rglob("private-*"))


def test_sticky_shared_spill_root_uses_private_child(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(0o1777)
    monkeypatch.setenv("TA_DUCKDB_TMP", str(root))
    temporary = query._spill_directory()
    try:
        assert stat.S_IMODE(Path(temporary.name).stat().st_mode) == 0o700
        assert stat.S_IMODE(root.stat().st_mode) == 0o1777
    finally:
        temporary.cleanup()


def test_shared_location_keeps_its_permissions(tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o777)
    root = parent / "owned"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("TA_DUCKDB_TMP", str(root))
    temporary = query._spill_directory()
    try:
        assert stat.S_IMODE(Path(temporary.name).stat().st_mode) == 0o700
        assert stat.S_IMODE(parent.stat().st_mode) == 0o777
    finally:
        temporary.cleanup()
