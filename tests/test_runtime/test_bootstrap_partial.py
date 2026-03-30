"""Tests for partial bootstrap behavior."""
import pytest

from genesis.runtime import GenesisRuntime


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
