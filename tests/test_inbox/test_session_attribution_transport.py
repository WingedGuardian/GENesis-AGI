"""Real inbox/manager/invoker flow with only the CLI transport made inert."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
from genesis.cc import invoker as invoker_module
from genesis.cc import transient_retry
from genesis.cc.invoker import CCInvoker
from genesis.cc.session_manager import SessionManager
from genesis.inbox.monitor import InboxMonitor
from genesis.observability.events import GenesisEventBus
from genesis.observability.session_context import get_session_id, session_scope
from tests.test_inbox import test_edge_cases as shared

db = shared.db
config = shared.config
inbox_dir = shared.inbox_dir
writer = shared.writer


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "retry", "error", "cancel"])
async def test_scan_through_real_invoker_to_persisted_outcome(
    mode,
    db,
    config,
    inbox_dir,
    writer,
    monkeypatch,
):
    response = (
        "# Inbox Evaluation\n\n## 1. Sample tool\n"
        "**Source:** <https://example.com/first>\n\n"
        + "Synthetic integration evidence. "
        * 10
        + "\n### Recommendation\n```yaml\naction: ADOPT\n"
        "next_step: Compare the documented integration interface.\nscope: V4\n```\n"
    )
    (inbox_dir / "Note.md").write_text("https://example.com/first")
    spawned, finished = [], []
    bus = GenesisEventBus()
    bus.enable_persistence(db)
    monkeypatch.setattr(invoker_module, "_runtime_event_bus", lambda: bus)
    monkeypatch.setattr(invoker_module, "_failure_event_state", {})

    async def spawn(*args, **kwargs):
        env = kwargs["env"]
        spawned.append(
            (
                get_session_id(),
                env["GENESIS_SESSION_ID"],
                env["GENESIS_SESSION_ORIGIN"],
                env.get("GENESIS_SESSION_SUPERVISED"),
            )
        )
        event = {
            "type": "result",
            "is_error": False,
            "result": response,
            "session_id": "reported-cli-session",
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "duration_ms": 1,
        }
        if mode in {"retry", "cancel"} and len(spawned) == 1:
            event.update(is_error=True, result="529 overloaded", api_error_status=529, num_turns=1)
        elif mode == "error":
            event.update(is_error=True, result="ordinary synthetic failure")
        return SimpleNamespace(
            pid=900000000 + len(spawned),
            returncode=0,
            communicate=AsyncMock(return_value=(json.dumps(event).encode(), b"")),
        )

    async def sleep(delay):
        if mode == "cancel":
            raise asyncio.CancelledError()

    async def passthrough(value, *args):
        return value

    monkeypatch.setattr(invoker_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(invoker_module, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(invoker_module, "_settings_env_pins", lambda *a: {})
    monkeypatch.setattr(invoker_module, "set_oom_score_adj", lambda *a: None)
    monkeypatch.setattr(invoker_module.oom, "capture_baseline", lambda **kw: None)
    monkeypatch.setattr(invoker_module.oom, "capture_finish", lambda *a, **kw: None)
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda value: (value, "claude"))
    monkeypatch.setattr(transient_retry, "_sleep", sleep)
    invoker = CCInvoker(claude_path="/no-cli-is-run")
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "_build_args", lambda *a, **kw: ["/no-cli-is-run"])
    monkeypatch.setattr(invoker, "verify_allowlist_enforceable", AsyncMock())
    monkeypatch.setattr(invoker, "_apply_login_fallback", passthrough)
    monkeypatch.setattr(invoker, "_with_cost_semantics", passthrough)
    for name in (
        "_notify_status_change",
        "_fire_downgrade_callback",
        "_fire_empty_output_callback",
    ):
        monkeypatch.setattr(invoker, name, AsyncMock())
    # Real _build_env, final _launch_env, parser, classifier, retry and event bus.
    manager = SessionManager(db=db, invoker=invoker)

    async def on_end(session_id):
        finished.append((session_id, get_session_id()))

    manager.add_on_end(on_end)
    monitor = InboxMonitor(
        db=db, invoker=invoker, session_manager=manager, config=config, writer=writer
    )
    dispatcher = SimpleNamespace(
        route=AsyncMock(
            return_value=AutonomousDispatchDecision(mode="cli", reason="fixture approval")
        ),
        approval_gate=SimpleNamespace(find_site_pending=AsyncMock(return_value=None)),
    )
    # check_once is driven explicitly below. The public bootstrap setter also
    # schedules an unrelated delayed _check_inbox that imports the live runtime.
    monitor._autonomous_dispatcher = dispatcher
    try:
        with session_scope("outer-session"):
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await monitor.check_once()
            else:
                result = await monitor.check_once()
                assert result.batches_dispatched == (mode in {"success", "retry"})
            assert get_session_id() == "outer-session"
        await bus.stop()
        sessions = await (
            await db.execute("SELECT id,status,origin_class FROM cc_sessions")
        ).fetchall()
        assert len(sessions) == 1
        manager_id = sessions[0]["id"]
        assert manager_id != "reported-cli-session"
        assert sessions[0]["origin_class"] == "external_untrusted"
        assert spawned == [(manager_id, manager_id, "external_untrusted", None)] * (
            2 if mode == "retry" else 1
        )
        assert dispatcher.route.await_count == 1
        assert finished == ([] if mode == "cancel" else [(manager_id, "outer-session")])
        rows = await (
            await db.execute("SELECT status,evaluated_content,response_path FROM inbox_items")
        ).fetchall()
        assert len(rows) == 1
        if mode in {"success", "retry"}:
            assert rows[0]["status"] == "completed"
            assert "https://example.com/first" in rows[0]["evaluated_content"]
            assert response in Path(rows[0]["response_path"]).read_text()
            followups = await (
                await db.execute(
                    "SELECT source_session FROM follow_ups WHERE source='inbox_evaluation'",
                )
            ).fetchall()
            assert [r["source_session"] for r in followups] == ["reported-cli-session"]
        else:
            assert rows[0]["status"] != "completed"
            assert not rows[0]["evaluated_content"]
        failures = await (
            await db.execute(
                "SELECT session_id FROM events WHERE event_type='cc.invocation_failed'",
            )
        ).fetchall()
        assert [r["session_id"] for r in failures] == ([] if mode == "success" else [manager_id])
    finally:
        await bus.stop()
