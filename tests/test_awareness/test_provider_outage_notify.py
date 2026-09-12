"""Tests for _check_provider_outage_notify — the dead-provider Telegram sweep.

The DECISION lives in `routing/escalation.py::sweep_due_notifications`; this
wrapper supplies the clock (the awareness tick) and the operator lever. These
tests pin four things: the lever's three modes actually change what is written
(off resolves, propose_only demotes, live notifies), the env kill switch wins
over the file, a failure never raises into the tick, and — the one that has
bitten this repo before — the tick REALLY CALLS the check, because a function
that exists but is never invoked ships green and inert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.mark.asyncio
async def test_db_none_is_a_noop(monkeypatch):
    sweep = AsyncMock()
    monkeypatch.setattr(
        "genesis.routing.escalation.sweep_due_notifications", sweep
    )
    await loop._check_provider_outage_notify(None)
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_mode_sweeps_at_critical(monkeypatch):
    sweep = AsyncMock(return_value=1)
    monkeypatch.setattr("genesis.routing.escalation.sweep_due_notifications", sweep)
    monkeypatch.setattr(
        "genesis.awareness.provider_notify_config.effective_mode", lambda: "live"
    )
    await loop._check_provider_outage_notify(object())
    sweep.assert_awaited_once()
    assert sweep.await_args.kwargs["priority"] == "critical"


@pytest.mark.asyncio
async def test_propose_only_demotes_to_high(monkeypatch):
    """propose_only must be visible on the dashboard but never reach Telegram —
    the delivery job polls only priority='critical'."""
    sweep = AsyncMock(return_value=1)
    monkeypatch.setattr("genesis.routing.escalation.sweep_due_notifications", sweep)
    monkeypatch.setattr(
        "genesis.awareness.provider_notify_config.effective_mode",
        lambda: "propose_only",
    )
    await loop._check_provider_outage_notify(object())
    assert sweep.await_args.kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_off_resolves_instead_of_sweeping(monkeypatch):
    """USER-DECIDED: off resolves open notify rows (loop contract — disabling
    never strands an alert), which is what makes off→on re-notify."""
    sweep = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr("genesis.routing.escalation.sweep_due_notifications", sweep)
    monkeypatch.setattr(
        "genesis.awareness.provider_notify_config.effective_mode", lambda: "off"
    )
    monkeypatch.setattr(loop, "_resolve_provider_outage_notify", resolve)
    await loop._check_provider_outage_notify(object())
    sweep.assert_not_awaited()
    resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_env_kill_switch_forces_off(monkeypatch):
    """The env lever must win over a `live` file — checked through the REAL
    config module, not a stubbed mode."""
    sweep = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr("genesis.routing.escalation.sweep_due_notifications", sweep)
    monkeypatch.setattr(loop, "_resolve_provider_outage_notify", resolve)
    monkeypatch.setenv("GENESIS_PROVIDER_NOTIFY_DISABLED", "1")
    await loop._check_provider_outage_notify(object())
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sweep_failure_never_raises_into_the_tick(monkeypatch):
    sweep = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("genesis.routing.escalation.sweep_due_notifications", sweep)
    monkeypatch.setattr(
        "genesis.awareness.provider_notify_config.effective_mode", lambda: "live"
    )
    await loop._check_provider_outage_notify(object())  # must not raise


def test_the_tick_actually_calls_the_check():
    """WIRED, not just existing — the failure mode this repo has shipped before
    (three features green-but-inert because a shell-tested read failed in the
    server sandbox / the call site was never added). The loop's call block is
    hand-maintained, so pin the call: the check's name must appear as an awaited
    call inside `_on_tick`'s body, in the every-tick (non-hourly) region.
    """
    import ast
    import inspect

    src = inspect.getsource(loop.AwarenessLoop._on_tick)
    tree = ast.parse("class _W:\n" + "\n".join(
        "    " + line for line in src.splitlines()
    ))
    awaited = [
        node.value.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "_check_provider_outage_notify" in awaited, (
        "_check_provider_outage_notify is not AWAITED inside _on_tick — either "
        "the call was removed (the sweep ships green and inert) or the await "
        "was dropped (the coroutine is created and never runs, which looks "
        "identical from outside)"
    )


# ── real-DB coverage for the write paths the mocked tests above cannot see ──


@pytest.mark.asyncio
async def test_off_resolver_touches_only_notify_rows(empty_db):
    """SHOULD-FIX from review: the resolver was the one WRITE path covered only
    by mocks — a typo in its discriminator key, its resolve_batch kwargs, or its
    row access would have shipped green. Runs the real function against a real
    DB: one failure row and one notify row unresolved; only the notify row may
    resolve, and it must carry the lever note.
    """
    import hashlib
    import json

    failure_hash = hashlib.sha256(b"provider_failure:prov-r").hexdigest()
    notify_hash = hashlib.sha256(b"provider_dead_notify:prov-r").hexdigest()
    await empty_db.execute(
        "INSERT INTO observations "
        "(id, source, type, content, priority, resolved, content_hash, created_at) "
        "VALUES ('f1', 'routing', 'provider_failure', ?, 'high', 0, ?, "
        "datetime('now', '-7200 seconds'))",
        (json.dumps({"provider": "prov-r", "first_trip_at": "x"}), failure_hash),
    )
    await empty_db.execute(
        "INSERT INTO observations "
        "(id, source, type, content, priority, resolved, content_hash, created_at) "
        "VALUES ('n1', 'routing', 'provider_failure', ?, 'critical', 0, ?, "
        "datetime('now', '-600 seconds'))",
        (json.dumps({"provider": "prov-r", "outage_started_at": "x"}), notify_hash),
    )
    await empty_db.commit()

    await loop._resolve_provider_outage_notify(empty_db)

    cur = await empty_db.execute(
        "SELECT id, resolved, resolution_notes FROM observations ORDER BY id"
    )
    rows = {r["id"]: r for r in await cur.fetchall()}
    assert rows["f1"]["resolved"] == 0, "the FAILURE row was wrongly resolved"
    assert rows["n1"]["resolved"] == 1, "the notify row was not resolved"
    assert "lever turned off" in (rows["n1"]["resolution_notes"] or "")


@pytest.mark.asyncio
async def test_live_mode_promotes_a_propose_only_row(empty_db, monkeypatch):
    """SHOULD-FIX from review: dedup keys exclude priority, so a notify row
    written at high under propose_only would satisfy skip_if_duplicate forever
    and upgrading the lever to live would silently never send the Telegram.
    The check must resolve the demoted row so the same tick rewrites it at
    critical.
    """
    import hashlib
    import json

    notify_hash = hashlib.sha256(b"provider_dead_notify:prov-up").hexdigest()
    failure_hash = hashlib.sha256(b"provider_failure:prov-up").hexdigest()
    for oid, h, content, prio in (
        ("f1", failure_hash, {"provider": "prov-up"}, "high"),
        ("n1", notify_hash, {"provider": "prov-up", "outage_started_at": "x"}, "high"),
    ):
        await empty_db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, resolved, content_hash, created_at) "
            "VALUES (?, 'routing', 'provider_failure', ?, ?, 0, ?, "
            "datetime('now', '-7200 seconds'))",
            (oid, json.dumps(content), prio, h),
        )
    await empty_db.commit()

    monkeypatch.setattr(
        "genesis.awareness.provider_notify_config.effective_mode", lambda: "live"
    )
    await loop._check_provider_outage_notify(empty_db)

    cur = await empty_db.execute(
        "SELECT priority, resolved FROM observations WHERE content_hash = ? "
        "ORDER BY created_at",
        (notify_hash,),
    )
    rows = await cur.fetchall()
    open_rows = [r for r in rows if r["resolved"] == 0]
    assert len(open_rows) == 1, "expected exactly one open notify row after the tick"
    assert open_rows[0]["priority"] == "critical", (
        "the propose_only row was not promoted — the Telegram would never send"
    )
