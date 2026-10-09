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


def _deep_report(ok: bool, failures=None, details=None):
    return git_health.GitHealthReport(
        ok=ok, failures=failures or [], details=details or {}, kind="deep", checked_at="t"
    )


@pytest.fixture(autouse=True)
def _reset_deep_guard():
    loop._last_git_deep_run_at = None
    loop._git_deep_task = None
    yield
    loop._last_git_deep_run_at = None
    loop._git_deep_task = None


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
    async def test_deep_alert_is_truthful_no_false_claims(self, monkeypatch):
        # The alert must surface the ACTUAL fsck stderr and must NOT repeat the
        # old hard-coded false narrative: "objects missing or corrupt" (it may be
        # any fsck failure) and "disables the REVERT_CODE lever" (the deep verdict
        # does not gate REVERT_CODE — the guardian preflight uses a live cheap
        # probe). Verified 2026-08-25.
        monkeypatch.setattr(
            git_health,
            "check_git_deep",
            AsyncMock(
                return_value=_deep_report(
                    False,
                    ["fsck_failed"],
                    {"fsck_stderr": "error: sentinel-xyz object corrupt marker"},
                )
            ),
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        obs = AsyncMock()
        monkeypatch.setattr(loop.observations, "create", obs)

        await loop._check_git_health_deep(object())

        content = obs.call_args.kwargs["content"]
        assert "sentinel-xyz" in content, "alert must show the real fsck stderr"
        assert "REVERT_CODE" not in content, "must not repeat the false REVERT_CODE claim"
        assert "missing or corrupt" not in content, "must not hard-code a corruption narrative"

    @pytest.mark.asyncio
    async def test_deep_alert_dedup_hash_stable_across_varying_stderr(self, monkeypatch):
        # The alert BODY now shows run-varying fsck stderr, but the dedup identity
        # must key on the stable failure-CLASS (create() dedups on content_hash) —
        # else two transient failures with differing stderr both insert, piling up
        # stale criticals the self-heal is meant to prevent. Lock the stable hash.
        hashes: list[str | None] = []

        async def _capture(*a, **k):
            hashes.append(k.get("content_hash"))
            return "id"

        monkeypatch.setattr(loop.observations, "create", _capture)
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)

        for stderr in ("error: unable to read aaaa", "error: unable to read bbbb"):
            loop._last_git_deep_run_at = None  # bypass the daily guard for the 2nd run
            monkeypatch.setattr(
                git_health,
                "check_git_deep",
                AsyncMock(
                    return_value=_deep_report(False, ["fsck_failed"], {"fsck_stderr": stderr})
                ),
            )
            await loop._check_git_health_deep(object())

        assert hashes[0] is not None, "must pass an explicit stable content_hash"
        assert hashes[0] == hashes[1], "dedup hash must be stable despite differing stderr"

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


# ── Self-healing + dedup (git false-alarm fix) ─────────────────────────────


@pytest.mark.asyncio
async def test_cheap_pass_auto_resolves_cheap_alerts_only(monkeypatch):
    monkeypatch.setattr(git_health, "check_git_cheap", AsyncMock(return_value=_report(True)))
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    resolve = AsyncMock(return_value=1)
    monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)

    await loop._check_git_health(object())

    resolve.assert_awaited_once()
    kw = resolve.call_args.kwargs
    assert kw["source"] == "git_health_monitor"
    assert kw["type"] == "infrastructure_alert"
    # A passing structural probe clears ONLY cheap-scan alerts — it cannot
    # verify content integrity, so deep alerts must survive it.
    assert kw["category"] == "git_cheap"


@pytest.mark.asyncio
async def test_cheap_pass_none_db_skips_resolve(monkeypatch):
    monkeypatch.setattr(git_health, "check_git_cheap", AsyncMock(return_value=_report(True)))
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    resolve = AsyncMock()
    monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)

    await loop._check_git_health(None)

    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_cheap_alert_carries_category_and_dedup(monkeypatch):
    monkeypatch.setattr(
        git_health, "check_git_cheap", AsyncMock(return_value=_report(False, ["config_invalid"]))
    )
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    obs = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", obs)

    await loop._check_git_health(object())

    kw = obs.call_args.kwargs
    assert kw["category"] == "git_cheap"
    # DB-level dedup: identical unresolved alerts must not stack (also the
    # only cross-process guard — two concurrent loops share the DB).
    assert kw["skip_if_duplicate"] is True


@pytest.mark.asyncio
async def test_cheap_resolve_error_never_raises(monkeypatch):
    monkeypatch.setattr(git_health, "check_git_cheap", AsyncMock(return_value=_report(True)))
    monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
    monkeypatch.setattr(
        loop.observations,
        "resolve_by_source_and_type",
        AsyncMock(side_effect=RuntimeError("db locked")),
    )

    # Auto-resolve failure must never break the tick.
    await loop._check_git_health(object())


class TestDeepSelfHeal:
    """Deep-scan pass resolves open DEEP alerts only (fsck verifies content;
    it proves nothing about cheap-probe failures like rootfs_readonly)."""

    @pytest.mark.asyncio
    async def test_deep_pass_auto_resolves_deep_alerts_only(self, monkeypatch):
        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(return_value=_deep_report(True))
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        resolve = AsyncMock(return_value=1)
        monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)

        await loop._check_git_health_deep(object())

        resolve.assert_awaited_once()
        kw = resolve.call_args.kwargs
        assert kw["source"] == "git_health_monitor"
        assert kw["type"] == "infrastructure_alert"
        # Scoped: fsck only READS the object store — a passing fsck must not
        # clear a live rootfs_readonly / structural cheap alert (Codex P1), and
        # git_deep_transient rows are event records that expire on their TTL.
        assert kw["category"] == "git_deep"

    @pytest.mark.asyncio
    async def test_deep_pass_none_db_skips_resolve(self, monkeypatch):
        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(return_value=_deep_report(True))
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        resolve = AsyncMock()
        monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)

        await loop._check_git_health_deep(None)

        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_deep_alert_carries_category_and_dedup(self, monkeypatch):
        monkeypatch.setattr(
            git_health,
            "check_git_deep",
            AsyncMock(return_value=_deep_report(False, ["fsck_failed"])),
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        obs = AsyncMock()
        monkeypatch.setattr(loop.observations, "create", obs)

        await loop._check_git_health_deep(object())

        kw = obs.call_args.kwargs
        assert kw["category"] == "git_deep"
        assert kw["skip_if_duplicate"] is True

    @pytest.mark.asyncio
    async def test_deep_resolve_error_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(return_value=_deep_report(True))
        )
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        monkeypatch.setattr(
            loop.observations,
            "resolve_by_source_and_type",
            AsyncMock(side_effect=RuntimeError("db locked")),
        )

        await loop._check_git_health_deep(object())


async def _rows(db):
    rows = await db.execute_fetchall(
        "SELECT category, priority, resolved, content, resolution_notes FROM observations "
        "WHERE source = 'git_health_monitor' ORDER BY created_at, rowid"
    )
    return [dict(r) for r in rows]


def _transient(lines="missing blob abc", checked_at="t"):
    return git_health.GitHealthReport(
        ok=True,
        failures=[],
        details={"fsck_transient": {"rc": 2, "lines": lines, "delay_s": 120}},
        kind="deep",
        checked_at=checked_at,
    )


class TestDeepRecheckAlerts:
    """#2745: a failure that passes its re-check is recorded once as 'high'
    (morning report, never Telegram); a reproduced one pages as before."""

    @pytest.fixture(autouse=True)
    def _no_verdict(self, monkeypatch):
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)

    async def _run(self, monkeypatch, db, report):
        monkeypatch.setattr(git_health, "check_git_deep", AsyncMock(return_value=report))
        loop._last_git_deep_run_at = None
        await loop._check_git_health_deep(db)

    @pytest.mark.asyncio
    async def test_transient_writes_one_high_row_and_clears_the_critical(self, monkeypatch, db):
        await self._run(monkeypatch, db, _deep_report(False, ["fsck_failed"], {"fsck_stderr": "x"}))
        await self._run(monkeypatch, db, _transient())
        rows = await _rows(db)
        assert [(r["category"], r["priority"], r["resolved"]) for r in rows] == [
            ("git_deep", "critical", 1),
            ("git_deep_transient", "high", 0),
        ]
        # The morning report and ego context cut content at 120-200 chars:
        # the verdict must come first.
        assert "PASSED on re-check" in rows[1]["content"][:120]
        assert "missing blob abc" in rows[1]["content"]

    @pytest.mark.asyncio
    async def test_race_transient_names_the_race(self, monkeypatch, db):
        report = _transient()
        report.details["fsck_transient"]["race"] = "present on lookup"
        report.details["fsck_transient"]["recheck_lines"] = "missing tree def"
        await self._run(monkeypatch, db, report)
        rows = await _rows(db)
        assert len(rows) == 1 and rows[0]["priority"] == "high"
        assert "scan race" in rows[0]["content"][:120]
        assert "PASSED" not in rows[0]["content"]
        assert "missing tree def" in rows[0]["content"]  # the re-check's own evidence

    @pytest.mark.asyncio
    async def test_each_transient_is_its_own_open_row(self, monkeypatch, db):
        # Recurrence must be visible: one row per event, none hiding another.
        await self._run(monkeypatch, db, _transient("first", checked_at="t1"))
        await self._run(monkeypatch, db, _transient("second", checked_at="t2"))
        rows = await _rows(db)
        assert [(r["category"], r["resolved"]) for r in rows] == [
            ("git_deep_transient", 0),
            ("git_deep_transient", 0),
        ]
        assert "first" in rows[0]["content"] and "second" in rows[1]["content"]

    @pytest.mark.asyncio
    async def test_same_event_retried_is_not_duplicated(self, monkeypatch, db):
        await self._run(monkeypatch, db, _transient(checked_at="t1"))
        await self._run(monkeypatch, db, _transient(checked_at="t1"))
        assert len(await _rows(db)) == 1

    @pytest.mark.asyncio
    async def test_clean_run_keeps_the_transient_record(self, monkeypatch, db):
        # A clean run must not erase the record before the morning report reads
        # it (that report reads only unresolved rows); the TTL retires it.
        await self._run(monkeypatch, db, _transient())
        await self._run(monkeypatch, db, _deep_report(True))
        rows = await _rows(db)
        assert [(r["category"], r["resolved"]) for r in rows] == [("git_deep_transient", 0)]

    @pytest.mark.asyncio
    async def test_transient_hash_never_collides_with_the_critical(self, monkeypatch, db):
        await self._run(monkeypatch, db, _transient())
        await self._run(monkeypatch, db, _deep_report(False, ["fsck_failed"], {"fsck_stderr": "x"}))
        rows = await _rows(db)
        assert [(r["category"], r["priority"]) for r in rows] == [
            ("git_deep_transient", "high"),
            ("git_deep", "critical"),
        ]

    @pytest.mark.asyncio
    async def test_reproduced_failure_pages_with_note(self, monkeypatch, db):
        await self._run(
            monkeypatch,
            db,
            _deep_report(
                False,
                ["fsck_failed"],
                {"fsck_stderr": "missing blob abc", "fsck_reproduced": True},
            ),
        )
        rows = await _rows(db)
        assert len(rows) == 1 and rows[0]["priority"] == "critical"
        assert "(reproduced on re-check)" in rows[0]["content"]
        assert "missing blob abc" in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_recheck_timeout_pages_with_note(self, monkeypatch, db):
        await self._run(
            monkeypatch,
            db,
            _deep_report(False, ["fsck_failed"], {"fsck_stderr": "e", "fsck_recheck": "timeout"}),
        )
        rows = await _rows(db)
        assert "(re-check did not complete: timeout)" in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_cancel_mid_run_writes_nothing(self, monkeypatch, db, caplog):
        import asyncio

        monkeypatch.setattr(
            git_health, "check_git_deep", AsyncMock(side_effect=asyncio.CancelledError)
        )
        with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
            await loop._check_git_health_deep(db)
        assert await _rows(db) == []
        assert not [r for r in caplog.records if r.levelname == "ERROR"]


class TestDeepDispatch:
    """The deep scan runs out-of-band: the tick never awaits it (#2745)."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_without_awaiting_and_is_single_flight(self, monkeypatch):
        import asyncio

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(_repo=None):
            started.set()
            await release.wait()
            return _deep_report(True)

        monkeypatch.setattr(git_health, "check_git_deep", _slow)
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", AsyncMock())

        loop._dispatch_git_health_deep(None)  # returns at once: not a coroutine
        first = loop._git_deep_task
        await asyncio.wait_for(started.wait(), 5)
        assert not first.done()
        loop._dispatch_git_health_deep(None)  # still running -> no second task
        assert loop._git_deep_task is first
        release.set()
        await asyncio.wait_for(first, 5)
        loop._dispatch_git_health_deep(None)  # done, but not due for 24 h
        assert loop._git_deep_task is first

    @pytest.mark.asyncio
    async def test_dispatch_starts_when_due(self, monkeypatch):
        deep = AsyncMock(return_value=_deep_report(True))
        monkeypatch.setattr(git_health, "check_git_deep", deep)
        monkeypatch.setattr(git_health, "write_git_health_verdict", lambda *a, **k: None)
        loop._last_git_deep_run_at = loop.time.monotonic() - loop._GIT_DEEP_INTERVAL_S - 1
        loop._dispatch_git_health_deep(None)
        await loop._git_deep_task
        deep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stalled_run_warns_once(self, caplog):
        import asyncio

        stuck = asyncio.get_running_loop().create_future()  # never completes
        loop._git_deep_task = stuck
        loop._last_git_deep_run_at = loop.time.monotonic() - loop._git_deep_stuck_s() - 1
        loop._git_deep_stuck_warned = False
        try:
            with caplog.at_level("WARNING", logger=loop.__name__):
                loop._dispatch_git_health_deep(None)
                loop._dispatch_git_health_deep(None)
            stalls = [r for r in caplog.records if "stalled" in r.getMessage()]
            assert len(stalls) == 1
            assert loop._git_deep_task is stuck  # no second run on top of it
        finally:
            stuck.cancel()
            loop._git_deep_stuck_warned = False


@pytest.mark.asyncio
async def test_request_stop_cancels_an_in_flight_scan():
    """A service stop cancels the deep scan, so it leaves no verdict or page."""
    import asyncio

    from genesis.awareness.loop import AwarenessLoop

    blocker = asyncio.Event()

    async def _long():
        await blocker.wait()

    loop._git_deep_task = asyncio.get_running_loop().create_task(_long())
    try:
        AwarenessLoop.request_stop(object.__new__(AwarenessLoop))
        # Bounded: if stop did not cancel, this fails on the timeout, never hangs.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(loop._git_deep_task), 5)
        assert loop._git_deep_task.cancelled()
    finally:
        loop._git_deep_task = None


@pytest.mark.asyncio
async def test_aborted_scan_writes_no_verdict_and_no_row(monkeypatch, db):
    """A service stop that kills fsck (CancelledError from check_git_deep) leaves
    the verdict file and the observations table untouched."""
    import asyncio

    verdicts = []
    monkeypatch.setattr(git_health, "check_git_deep", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr(
        git_health, "write_git_health_verdict", lambda r, *a, **k: verdicts.append(r)
    )
    loop._last_git_deep_run_at = None
    with pytest.raises(asyncio.CancelledError):
        await loop._check_git_health_deep(db)
    assert verdicts == []
    assert await _rows(db) == []
