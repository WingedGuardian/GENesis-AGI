"""Tests for transcript archival (gzip old .jsonl files)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.surplus.maintenance import archive_old_transcripts


@pytest.fixture
def tmp_sessions(tmp_path: Path) -> Path:
    """Create a temp directory simulating background-sessions/."""
    return tmp_path


def _create_file(base: Path, name: str, content: str, age_days: int) -> Path:
    """Create a file with a given mtime age."""
    import os
    f = base / name
    f.write_text(content)
    mtime = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    os.utime(f, (mtime, mtime))
    return f


@pytest.mark.asyncio
async def test_archives_old_files(tmp_sessions):
    """Files older than threshold are gzipped."""
    _create_file(tmp_sessions, "old.jsonl", '{"a":1}\n', age_days=120)
    count = await archive_old_transcripts(tmp_sessions, older_than_days=90)
    assert count == 1
    assert (tmp_sessions / "old.jsonl.gz").exists()
    assert not (tmp_sessions / "old.jsonl").exists()

    # Verify contents
    with gzip.open(tmp_sessions / "old.jsonl.gz", "rt") as f:
        assert f.read() == '{"a":1}\n'


@pytest.mark.asyncio
async def test_spares_recent_files(tmp_sessions):
    """Files younger than threshold are left alone."""
    _create_file(tmp_sessions, "recent.jsonl", '{"b":2}\n', age_days=30)
    count = await archive_old_transcripts(tmp_sessions, older_than_days=90)
    assert count == 0
    assert (tmp_sessions / "recent.jsonl").exists()


@pytest.mark.asyncio
async def test_skips_nonexistent_directory(tmp_path):
    """Missing directory returns 0 without error."""
    count = await archive_old_transcripts(
        tmp_path / "nonexistent", older_than_days=90,
    )
    assert count == 0


@pytest.mark.asyncio
async def test_ignores_non_jsonl_files(tmp_sessions):
    """Only .jsonl files are targeted."""
    _create_file(tmp_sessions, "old.log", "log content\n", age_days=120)
    count = await archive_old_transcripts(tmp_sessions, older_than_days=90)
    assert count == 0
    assert (tmp_sessions / "old.log").exists()


@pytest.mark.asyncio
async def test_skips_already_compressed(tmp_sessions):
    """Already-compressed .jsonl.gz files are not touched."""
    gz_path = tmp_sessions / "already.jsonl.gz"
    with gzip.open(gz_path, "wt") as f:
        f.write("compressed\n")
    import os
    mtime = (datetime.now(UTC) - timedelta(days=120)).timestamp()
    os.utime(gz_path, (mtime, mtime))

    count = await archive_old_transcripts(tmp_sessions, older_than_days=90)
    assert count == 0  # .gz files don't match *.jsonl glob
