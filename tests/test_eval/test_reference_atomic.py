"""Atomic draft publication: offline failures must never claim a partial final path."""

import errno
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest


@pytest.mark.parametrize(
    "fault",
    [
        "serialize",
        "write",
        "flush",
        "close",
        "fsync",
        "publish",
        "competitor",
        "cleanup",
        "success",
    ],
)
async def test_atomic_publication_failure_population(tmp_path, monkeypatch, fault):
    from genesis.eval import reflection_golden_set as generator

    real_connect = aiosqlite.connect
    monkeypatch.setattr(
        generator.aiosqlite, "connect", lambda *a, **k: real_connect(tmp_path / "source.db")
    )
    observation = dict(
        id="one", content="Synthetic", created_at="2026-10-02", priority=1, retrieved_count=0
    )
    sample = AsyncMock(return_value=[observation])
    monkeypatch.setattr(generator, "_sample_observations", sample)
    monkeypatch.setattr(generator, "_get_session_context", AsyncMock(return_value="Context"))
    monkeypatch.setattr(
        generator, "_grade_observation", AsyncMock(return_value=(0.9, "Synthetic", "stub"))
    )
    output = tmp_path / "draft.jsonl"
    if fault == "serialize":
        observation["id"] = b"unsupported JSON value"
    real_open = Path.open
    real_temp = tempfile.NamedTemporaryFile
    real_fsync = os.fsync
    real_link = os.link
    real_unlink = Path.unlink
    stages = []

    class FaultFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            result = self.file.__exit__(*args)
            if fault == "close":
                raise OSError(errno.EIO, "Injected close failure")
            return result

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, content):
            if fault == "write":
                raise OSError(errno.ENOSPC, "Injected write failure")
            return self.file.write(content)

        def flush(self):
            if fault == "flush":
                raise OSError(errno.ENOSPC, "Injected flush failure")
            return self.file.flush()

    def path_open(self, *args, **kwargs):
        file = real_open(self, *args, **kwargs)
        return FaultFile(file) if self == output and args and args[0] == "x" else file

    def temporary(*args, **kwargs):
        return FaultFile(real_temp(*args, **kwargs))

    def fsync(fd):
        stages.append("sync")
        assert not output.exists()
        if fault == "fsync":
            raise OSError(errno.EIO, "Injected fsync failure")
        return real_fsync(fd)

    def link(source, target, *args, **kwargs):
        stages.append("publish")
        assert stages == ["sync", "publish"]
        assert not output.exists()
        # A complete, independently readable file exists before publication.
        rows = [
            json.loads(line)
            for line in Path(source).read_text().splitlines()
            if not line.startswith("#")
        ]
        assert len(rows) == 1 and rows[0]["proposed_passed"] is True
        assert "user_passed" not in rows[0]
        if fault == "publish":
            raise OSError(errno.EPERM, "Injected unsupported publication")
        if fault == "competitor":
            output.write_text("competing writer")
        return real_link(source, target, *args, **kwargs)

    def unlink(self, *args, **kwargs):
        if fault == "cleanup" and self.name.startswith(".reflection-draft-"):
            raise OSError(errno.EACCES, "Injected cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(Path, "unlink", unlink)
    if fault in ("cleanup", "success"):
        summary = await generator.generate_golden_set(1, output)
        assert summary["graded"] == 1 and output.exists()
        assert stages == ["sync", "publish"]
    else:
        expected = (
            TypeError
            if fault == "serialize"
            else FileExistsError
            if fault == "competitor"
            else OSError
        )
        with pytest.raises(expected):
            await generator.generate_golden_set(1, output)
        if fault == "competitor":
            assert output.read_text() == "competing writer"
        else:
            assert not output.exists()
    leftovers = list(tmp_path.glob(".reflection-draft-*"))
    assert bool(leftovers) == (fault == "cleanup")
    # Repair the injected fault and retry exactly the same output path.
    if fault not in ("cleanup", "success", "competitor"):
        observation["id"] = "one"
        monkeypatch.setattr(Path, "open", real_open)
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", real_temp)
        monkeypatch.setattr(os, "fsync", real_fsync)
        monkeypatch.setattr(os, "link", real_link)
        assert (await generator.generate_golden_set(1, output))["graded"] == 1
    for path in leftovers:
        real_unlink(path)


def test_terminated_writer_does_not_claim_destination(tmp_path):
    import subprocess
    import sys

    script = tmp_path / "terminated_writer.py"
    script.write_text("""
import asyncio, os, sys, tempfile
from pathlib import Path
from unittest.mock import AsyncMock
import aiosqlite
from genesis.eval import reflection_golden_set as generator
root = Path(sys.argv[1])
real_connect = aiosqlite.connect
real_temp = tempfile.NamedTemporaryFile
generator.aiosqlite.connect = lambda *a, **k: real_connect(root / 'source.db')
generator._sample_observations = AsyncMock(return_value=[dict(id='one',content='Synthetic',created_at='2026-10-02',priority=1,retrieved_count=0)])
generator._get_session_context = AsyncMock(return_value='Context')
generator._grade_observation = AsyncMock(return_value=(0.9,'Synthetic','stub'))
class TerminatedFile:
    def __init__(self,file): self.file=file
    def __enter__(self): return self
    def __exit__(self,*args): return self.file.__exit__(*args)
    def __getattr__(self,name): return getattr(self.file,name)
    def write(self,content):
        self.file.write(content)
        self.file.flush()
        os._exit(73)  # Abrupt process exit skips finally cleanup.
def temporary(*a,**k): return TerminatedFile(real_temp(*a,**k))
tempfile.NamedTemporaryFile = temporary
asyncio.run(generator.generate_golden_set(1,root/'draft.jsonl'))
""")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 73, result.stderr
    assert not (tmp_path / "draft.jsonl").exists()
    staging = list(tmp_path.glob(".reflection-draft-*.tmp"))
    assert len(staging) == 1 and staging[0].stat().st_size > 0
    staging[0].unlink()  # Only this test's known crash artifact.


async def test_two_generators_publish_exactly_one_complete_draft(tmp_path, monkeypatch):
    import asyncio

    from genesis.eval import reflection_golden_set as generator

    real_connect = aiosqlite.connect
    monkeypatch.setattr(
        generator.aiosqlite, "connect", lambda *a, **k: real_connect(tmp_path / "source.db")
    )
    monkeypatch.setattr(
        generator,
        "_sample_observations",
        AsyncMock(
            return_value=[
                dict(
                    id="one",
                    content="Synthetic",
                    created_at="2026-10-02",
                    priority=1,
                    retrieved_count=0,
                )
            ]
        ),
    )
    monkeypatch.setattr(generator, "_get_session_context", AsyncMock(return_value="Context"))
    arrivals = 0
    ready = asyncio.Event()

    async def grade(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return 0.9, "Synthetic", "stub"

    monkeypatch.setattr(generator, "_grade_observation", grade)
    output = tmp_path / "draft.jsonl"
    results = await asyncio.gather(
        generator.generate_golden_set(1, output),
        generator.generate_golden_set(1, output),
        return_exceptions=True,
    )
    assert sum(isinstance(result, FileExistsError) for result in results) == 1
    assert sum(isinstance(result, dict) and result["graded"] == 1 for result in results) == 1
    rows = [
        json.loads(line) for line in output.read_text().splitlines() if not line.startswith("#")
    ]
    assert len(rows) == 1 and rows[0]["proposed_passed"] is True
    assert not list(tmp_path.glob(".reflection-draft-*.tmp"))
