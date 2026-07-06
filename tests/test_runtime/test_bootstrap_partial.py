"""Tests for partial bootstrap behavior."""
import json

import pytest

from genesis.runtime import GenesisRuntime
from genesis.runtime._capabilities import _write_bootstrap_manifest_file


@pytest.mark.asyncio
async def test_bootstrap_db_failure_not_bootstrapped():
    """If db init fails, is_bootstrapped must be False."""
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {"db": "failed", "observability": "ok", "router": "ok"}
    rt._bootstrapped = False
    critical_ok = all(
        rt._bootstrap_manifest.get(name) == "ok"
        for name in GenesisRuntime._CRITICAL_SUBSYSTEMS
    )
    rt._bootstrapped = critical_ok
    assert not rt.is_bootstrapped


@pytest.mark.asyncio
async def test_bootstrap_all_critical_ok():
    """If db, observability, router all succeed, is_bootstrapped is True."""
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {"db": "ok", "observability": "ok", "router": "ok", "outreach": "failed"}
    rt._bootstrapped = False
    critical_ok = all(
        rt._bootstrap_manifest.get(name) == "ok"
        for name in GenesisRuntime._CRITICAL_SUBSYSTEMS
    )
    rt._bootstrapped = critical_ok
    assert rt.is_bootstrapped


def test_write_bootstrap_manifest_file_round_trips(tmp_path, monkeypatch):
    """The persisted manifest is verbatim — the MCP reader gets exact fidelity."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_mode = "full"
    rt._bootstrap_manifest = {
        "db": "ok", "outreach": "degraded: no token", "voice": "failed: no key",
    }
    _write_bootstrap_manifest_file(rt)

    written = json.loads((tmp_path / ".genesis" / "bootstrap_manifest.json").read_text())
    assert written["bootstrapped"] is True
    assert written["manifest"] == rt._bootstrap_manifest  # no lossy mapping
    assert written["persisted_at"]


def test_write_bootstrap_manifest_file_skips_readonly(tmp_path, monkeypatch):
    """A readonly probe must not clobber the primary runtime's manifest file."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_mode = "readonly"
    rt._bootstrap_manifest = {"db": "ok"}
    _write_bootstrap_manifest_file(rt)
    assert not (tmp_path / ".genesis" / "bootstrap_manifest.json").exists()


@pytest.mark.asyncio
async def test_bootstrap_noncritical_failure_still_bootstrapped():
    """If outreach fails but critical ok, still bootstrapped."""
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {
        "db": "ok", "observability": "ok", "router": "ok",
        "outreach": "failed", "recon": "failed",
    }
    rt._bootstrapped = False
    critical_ok = all(
        rt._bootstrap_manifest.get(name) == "ok"
        for name in GenesisRuntime._CRITICAL_SUBSYSTEMS
    )
    rt._bootstrapped = critical_ok
    assert rt.is_bootstrapped
