"""Read-only local peer API readiness, distinct from historical boot results."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from genesis.db.connection import open_ro_connection
from genesis.peers.auth import configuration_state
from genesis.peers.conversation import ConversationRefusal, ConversationService
from genesis.peers.registry import PeerRegistry


def unknown(reason="not_sampled", *, source="live") -> dict:
    return dict(status="unknown", reasons=[reason], checked_at=None, source=source,
                pid=os.getpid(), runtime_ready=False, task_service_ready=False)


async def evaluate(app) -> dict:
    """Bounded snapshot of actual app configuration and admission dependencies."""
    result = unknown()
    try:
        async with asyncio.timeout(2):
            await _evaluate(app, result)
    except TimeoutError:
        result.update(status="unknown", reasons=["check_timeout"])
    except Exception:
        # Paths, tokens and raw database errors must not enter health output.
        result.update(status="unknown", reasons=["check_unavailable"])
    result["checked_at"] = datetime.now(UTC).isoformat()
    return result


async def _evaluate(app, result):
    from genesis.runtime import GenesisRuntime

    registry = app.config.get("GENESIS_PEER_REGISTRY") or PeerRegistry()
    runtime = app.config.get("GENESIS_PEER_RUNTIME") or GenesisRuntime.peek()
    db = await open_ro_connection(registry.path, busy_timeout_ms=250)
    try:
        # Both SELECTs must see one revision, even during owner reconfiguration.
        await db.execute("BEGIN")
        row = await (await db.execute("SELECT * FROM peer_settings WHERE id=1")).fetchone()
        settings = dict(row) if row else {"mode": "disabled"}
        rows = [dict(r) for r in await (await db.execute("SELECT * FROM peers")).fetchall()]
        reason, _ = configuration_state(settings, rows, app=app)
        if reason:
            result.update(status="disabled" if reason == "disabled" else "degraded",
                          reasons=[reason])
            return
        if "agent_api" not in app.blueprints:
            result.update(status="degraded", reasons=["routes_missing"])
            return
        if runtime is None or getattr(runtime, "_db", None) is None:
            result.update(status="degraded", reasons=["runtime_missing"])
            return
        result["runtime_ready"] = True
        service = ConversationService(registry, runtime)
        try:
            runner = service.runner
            await service._same_database(db)
        except ConversationRefusal:
            result.update(status="degraded", reasons=["task_service_unavailable"])
            return
        if runner.closing:
            result.update(status="degraded", reasons=["runner_closing"])
            return
        result.update(status="healthy", reasons=[], task_service_ready=True)
    finally:
        await db.close()
