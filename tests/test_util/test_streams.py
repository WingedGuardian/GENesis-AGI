"""Stream retention limits preserve byte counts and the legacy executor alias."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.util.streams import DEFAULT_STREAM_LIMIT, read_limited


@pytest.mark.asyncio
@pytest.mark.parametrize("size,limit", [(0, 4), (3, 4), (4, 4), (10, 4), (20000, 9000), (10, 0), (10, -1)])
async def test_prefix_and_full_drained_count(size, limit):
    stream = asyncio.StreamReader()
    content = (b"abcdef" * ((size + 5) // 6))[:size]
    stream.feed_data(content)
    stream.feed_eof()
    retained, total = await read_limited(stream, limit)
    assert retained == content[:max(0, limit)]
    assert total == size
    assert await stream.read() == b""


@pytest.mark.asyncio
async def test_default_and_legacy_alias_share_one_cap():
    from genesis.autonomy.executor import deterministic

    assert deterministic._read_limited is read_limited
    assert deterministic._MAX_STREAM_BYTES == DEFAULT_STREAM_LIMIT == 2 * 1024 * 1024
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * (DEFAULT_STREAM_LIMIT + 1))
    stream.feed_eof()
    retained, total = await deterministic._read_limited(stream)
    assert len(retained) == DEFAULT_STREAM_LIMIT
    assert total == DEFAULT_STREAM_LIMIT + 1


def test_lightweight_import_does_not_load_executor():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys;sys.path.insert(0,sys.argv[1]);"
         "from genesis.util.streams import read_limited;"
         "assert not any(n=='genesis.autonomy.executor' or n.startswith('genesis.autonomy.executor.') for n in sys.modules)",
         str(root / "src")], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
