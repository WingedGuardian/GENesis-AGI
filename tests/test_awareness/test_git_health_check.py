"""Tests for the awareness-tick git-health check (_check_git_health, F.1).

The per-tick probe writes a shared-mount verdict and, on failure, raises a
cooldown-damped CRITICAL observation pointing at scripts/git_repair.py. Mirrors
the WAL-health test shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop
from genesis.observability import git_health


def _report(ok: bool, failures=None):
    return git_health.GitHealthReport(
        ok=ok, failures=failures or [], details={}, kind="cheap", checked_at="t"
    )


@pytest.fixture(autouse=True)
def _reset_cooldown():
    loop._last_git_alert_at = None
    yield
    loop._last_git_alert_at = None


@pytest.mark.asyncio
async def test_healthy_writes_verdict_no_alert(monkeypatch):
    monkeypatch.setattr(git_health, "check_git_cheap", AsyncMock(return_value=_report(True)))
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    obs = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", obs)

    await loop._check_git_health(object())

    obs.assert_not_called()


@pytest.mark.asyncio
async def test_unhealthy_raises_critical_observation(monkeypatch):
    monkeypatch.setattr(
        git_health, "check_git_cheap", AsyncMock(return_value=_report(False, ["rootfs_readonly"]))
    )
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    obs = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", obs)

    await loop._check_git_health(object())

    obs.assert_called_once()
    kw = obs.call_args.kwargs
    assert kw["priority"] == "critical"
    assert kw["source"] == "git_health_monitor"
    assert kw["type"] == "infrastructure_alert"
    assert "recovery-and-portability" in kw["content"]
    assert "rootfs_readonly" in kw["content"]


@pytest.mark.asyncio
async def test_verdict_written_even_when_unhealthy(monkeypatch):
    monkeypatch.setattr(
        git_health, "check_git_cheap", AsyncMock(return_value=_report(False, ["config_invalid"]))
    )
    calls = []
    monkeypatch.setattr(
        git_health, "write_git_health_verdict", lambda rep, *a, **k: calls.append(rep)
    )
    monkeypatch.setattr(loop.observations, "create", AsyncMock())

    await loop._check_git_health(object())

    assert len(calls) == 1
    assert calls[0].failures == ["config_invalid"]


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(monkeypatch):
    monkeypatch.setattr(
        git_health, "check_git_cheap", AsyncMock(return_value=_report(False, ["head_unresolvable"]))
    )
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    obs = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", obs)

    await loop._check_git_health(object())
    await loop._check_git_health(object())

    obs.assert_called_once()  # second call damped by cooldown


@pytest.mark.asyncio
async def test_none_db_no_crash(monkeypatch):
    monkeypatch.setattr(
        git_health, "check_git_cheap", AsyncMock(return_value=_report(False, ["config_invalid"]))
    )
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    # db is None → no observation attempted, no crash.
    await loop._check_git_health(None)


@pytest.mark.asyncio
async def test_probe_exception_never_raises(monkeypatch):
    monkeypatch.setattr(git_health, "check_git_cheap", AsyncMock(side_effect=RuntimeError("boom")))
    # Must not propagate — a crashing probe must never break the tick.
    await loop._check_git_health(object())


def _deep_report(ok: bool, failures=None):
    return git_health.GitHealthReport(
        ok=ok, failures=failures or [], details={}, kind="deep", checked_at="t"
    )


@pytest.fixture(autouse=True)
def _reset_deep_guard():
    loop._last_git_deep_run_at = None
    yield
    loop._last_git_deep_run_at = None


class TestDeepCheck:
    """The awareness-loop-driven daily `git fsck --full` (F.1). Loop-driven so it
    survives a router-degraded startup; self-guards to a ~daily cadence."""

    @pytest.mark.asyncio
    async def test_healthy_writes_verdict_no_alert(self, monkeypatch):
        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(return_value=_deep_report(True))
        )
        verdicts = []
        monkeypatch.setattr(
            git_health, "write_git_health_verdict", lambda rep, *a, **k: verdicts.append(rep)
        )
        obs = AsyncMock()
        monkeypatch.setattr(loop.observations, "create", obs)

        await loop._check_git_health_deep(object())

        assert len(verdicts) == 1 and verdicts[0].kind == "deep"
        obs.assert_not_called()

    @pytest.mark.asyncio
    async def test_unhealthy_raises_critical_observation(self, monkeypatch):
        monkeypatch.setattr(
            git_health,
            "check_git_deep",
            AsyncMock(return_value=_deep_report(False, ["fsck_failed"])),
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        obs = AsyncMock()
        monkeypatch.setattr(loop.observations, "create", obs)

        await loop._check_git_health_deep(object())

        obs.assert_called_once()
        kw = obs.call_args.kwargs
        assert kw["priority"] == "critical"
        assert kw["source"] == "git_health_monitor"
        assert "fsck --full" in kw["content"]
        assert "fsck_failed" in kw["content"]

    @pytest.mark.asyncio
    async def test_daily_guard_skips_second_run_within_window(self, monkeypatch):
        deep = AsyncMock(return_value=_deep_report(True))
        monkeypatch.setattr(git_health, "check_git_deep", deep)
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        monkeypatch.setattr(loop.observations, "create", AsyncMock())

        await loop._check_git_health_deep(object())  # first tick this boot → runs
        await loop._check_git_health_deep(object())  # within 24h → skipped

        deep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runs_again_after_interval_elapses(self, monkeypatch):
        deep = AsyncMock(return_value=_deep_report(True))
        monkeypatch.setattr(git_health, "check_git_deep", deep)
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        monkeypatch.setattr(loop.observations, "create", AsyncMock())

        await loop._check_git_health_deep(object())
        # Simulate >24h since the last run.
        loop._last_git_deep_run_at -= loop._GIT_DEEP_INTERVAL_S + 1
        await loop._check_git_health_deep(object())

        assert deep.await_count == 2

    @pytest.mark.asyncio
    async def test_none_db_still_runs_scan_no_observation(self, monkeypatch):
        # Degraded startup (no db): the scan + verdict still run (the whole point
        # of loop-driving it); only the observation is skipped.
        deep = AsyncMock(return_value=_deep_report(False, ["fsck_failed"]))
        monkeypatch.setattr(git_health, "check_git_deep", deep)
        verdicts = []
        monkeypatch.setattr(
            git_health, "write_git_health_verdict", lambda rep, *a, **k: verdicts.append(rep)
        )
        await loop._check_git_health_deep(None)  # must not crash
        deep.assert_awaited_once()
        assert len(verdicts) == 1

    @pytest.mark.asyncio
    async def test_scan_exception_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(side_effect=RuntimeError("boom"))
        )
        await loop._check_git_health_deep(object())  # swallowed — never breaks the tick
