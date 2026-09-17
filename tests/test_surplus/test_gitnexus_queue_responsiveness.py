"""Scheduling an index must not stall Genesis while the marker store is busy."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from genesis.runtime import GenesisRuntime
from genesis.surplus.jobs.gitnexus import run_gitnexus_reindex


@pytest.mark.asyncio
async def test_marker_wait_leaves_event_loop_responsive(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "index_marker", root / "scripts/lib/index_marker.py"
    )
    marker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(marker)
    monkeypatch.setitem(sys.modules, "index_marker", marker)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    helper = tmp_path / "genesis/scripts/lib/index_marker.py"
    helper.parent.mkdir(parents=True)
    helper.touch()
    runtime = SimpleNamespace(paused=False, record_job_success=Mock(), record_job_failure=Mock())
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: runtime)
    heartbeat = Event()
    observed = []
    write = marker.write_marker

    def waiting_write(*args, **kwargs):
        observed.append(heartbeat.wait(0.5))
        return write(*args, **kwargs)

    monkeypatch.setattr(marker, "write_marker", waiting_write)
    timer = asyncio.get_running_loop().call_later(0.05, heartbeat.set)
    try:
        await run_gitnexus_reindex()
    finally:
        timer.cancel()
    assert observed == [True], "marker I/O prevented the scheduled heartbeat from running"
    assert marker.list_markers()[0]["tools"] == "gitnexus"
    runtime.record_job_success.assert_called_once_with("gitnexus_reindex")
    runtime.record_job_failure.assert_not_called()
