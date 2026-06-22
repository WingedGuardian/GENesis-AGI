"""Tests for the scheduled skill-security scan recon job."""

from __future__ import annotations

import pytest

from genesis.recon.skill_security_scan_job import SkillSecurityScanJob


def test_resolve_bin_prefers_explicit_then_env(monkeypatch):
    monkeypatch.setenv("SKILLSPECTOR_BIN", "/from/env/skillspector")
    # Explicit bin wins over env.
    assert SkillSecurityScanJob(db=None, skillspector_bin="/explicit/ss")._resolve_bin() == "/explicit/ss"
    # Env wins when no explicit bin.
    assert SkillSecurityScanJob(db=None)._resolve_bin() == "/from/env/skillspector"


def test_resolve_bin_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.delenv("SKILLSPECTOR_BIN", raising=False)
    import genesis.recon.skill_security_scan_job as mod

    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mod, "_known_install_bin", lambda: None)
    assert SkillSecurityScanJob(db=None)._resolve_bin() is None


@pytest.mark.asyncio
async def test_run_skips_gracefully_when_binary_missing(monkeypatch):
    job = SkillSecurityScanJob(db=None)
    monkeypatch.setattr(job, "_resolve_bin", lambda: None)
    result = await job.run()
    # Missing scanner must NOT crash the scheduler — it returns a clean no-op summary.
    assert result["total_findings"] == 0
    assert result.get("skipped")


@pytest.mark.asyncio
async def test_run_delegates_and_summarizes(monkeypatch):
    """run() resolves the binary, scans, and reports the untrusted-filed count."""
    import genesis.recon.skill_security_scan_job as mod
    from genesis.security.skill_scan import ScanResult

    job = SkillSecurityScanJob(db=object(), skillspector_bin="/x/ss")
    monkeypatch.setattr(mod, "discover_skill_dirs", lambda _roots: ["/x/bad", "/x/ok"])
    monkeypatch.setattr(mod, "_load_trusted_names", lambda _p: {"ok"})
    monkeypatch.setattr(mod, "_repo_skill_roots", lambda: [])

    async def fake_scan_and_store(db, dirs, *, scanner, storer, trusted, **kw):
        # Mimic the real shape: one untrusted finding, one trusted (skipped).
        return [
            ScanResult(name="bad", source="/x/bad", score=100, severity="CRITICAL", recommendation="x"),
            ScanResult(name="ok", source="/x/ok", score=100, severity="CRITICAL", recommendation="x"),
        ]

    monkeypatch.setattr(mod, "scan_and_store", fake_scan_and_store)
    monkeypatch.setattr(mod, "is_trusted", lambda d, **kw: d.name == "ok")

    result = await job.run()
    assert result["skills_scanned"] == 2
    assert result["total_findings"] == 1  # only the untrusted "bad" counts
