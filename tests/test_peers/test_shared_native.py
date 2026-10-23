"""Actual authenticated A2A/native CLI flow using synthetic local providers."""

import asyncio
import json
import shutil
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc import roster
from genesis.cc.direct_session import DirectSessionRequest
from genesis.cc.invoker import CCInvoker
from genesis.cc.peer_segment import _INVOCATION_UNITS
from genesis.cc.provider_readiness import READY
from genesis.content.formatter import ContentFormatter
from genesis.outreach.config import QUIET_HOURS_DISABLED, OutreachConfig
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.scheduler import OutreachScheduler
from genesis.peers.conversation import ConversationRefusal
from tests.test_peers.test_conversation import ROOT, message, sdk_client, sdk_send, sdk_settle
from tests.test_peers.test_conversation import bridge as bridge
from tests.test_peers.test_conversation import http_bridge as http_bridge


@pytest.mark.skipif(shutil.which("claude") is None, reason="native Claude CLI unavailable")
async def test_authenticated_shared_native_reply_continuation_and_owner_handoff(http_bridge, monkeypatch):
    from a2a.types import TaskState

    b, app, base, _ = http_bridge
    monkeypatch.setattr("genesis.cc.direct_session._BG_CC_TMP_ROOT", b.home / "sessions")
    monkeypatch.setenv("GENESIS_BIG_TMP", str(b.home / "probes"))
    config = b.home / "cli-config"
    config.mkdir()
    private = "SYNTHETIC_PRIVATE_OWNER_CANARY_414"
    attachment = b.home / "private-attachment.txt"
    attachment.write_text(private)
    (config / "CLAUDE.md").write_text(private)
    monkeypatch.setenv("HOME", str(b.home))
    monkeypatch.setenv("GENESIS_HOME", str(b.home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "synthetic-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    bodies = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "count_tokens" in self.path:
                raw, content_type = b'{"input_tokens":10}', "application/json"
            else:
                bodies.append(body)
                text = READY if READY in json.dumps(body["messages"]) else "Shared synthetic reply"
                msg = {"id": "msg_fixture", "type": "message", "role": "assistant",
                    "model": "claude-sonnet-4-6", "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
                events = [
                    ("message_start", {"type": "message_start", "message": msg}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                raw = "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events).encode()
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as provider:
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(roster, "load_roster", lambda: {"default": "fixture", "models": {
                "fixture": {"anthropic_base_url": f"http://127.0.0.1:{provider.server_port}",
                    "model_id": "synthetic-model", "auth_env": "SYNTHETIC_PROVIDER_KEY"},
            }})
            normal_build, fake = b.runner._build_invocation, b.invoker
            def forbidden(*args, **kwargs):
                raise AssertionError("Shared request reached private owner builder")
            monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", forbidden)
            monkeypatch.setattr(b.runner, "_build_invocation", forbidden)
            b.runtime._context_injector = SimpleNamespace(inject=AsyncMock(side_effect=forbidden))
            observer = AsyncMock()
            native = CCInvoker(claude_path=shutil.which("claude"), on_cc_status_change=observer)
            native._last_was_error = True
            b.runner._invoker = b.runtime._cc_invoker = native
            async with b.registry.transaction() as db:
                await db.execute("UPDATE peers SET same_owner=0 WHERE peer_id='alpha'")
            await b.registry.grant("alpha", "conversation_shared", "allow")
            async with sdk_client(base) as (client, transport):
                first = message(timeout_s=45)
                first["message"]["parts"][0]["text"] = f"Public question. Consider @{attachment}"
                task = await sdk_send(transport, first)
                # Wait on native completion itself; SDK polling exercises projection afterwards.
                active = b.runner._active.get(task.id)
                if active:
                    await asyncio.wait_for(asyncio.shield(active), 50)
                done = await sdk_settle(transport, task.id)
                assert done.status.state == TaskState.TASK_STATE_COMPLETED
                result = await client.get(base + ROOT + f"/tasks/{done.id}/result")
                assert result.status_code == 200 and result.text == "Shared synthetic reply"
                assert (await sdk_send(transport, first)).id == done.id
                follow = await sdk_send(transport, message(context=done.context_id, timeout_s=45))
                active = b.runner._active.get(follow.id)
                if active:
                    await asyncio.wait_for(asyncio.shield(active), 50)
                assert (await sdk_settle(transport, follow.id)).status.state == TaskState.TASK_STATE_COMPLETED
                assert (await client.get(base + ROOT + "/outbox")).status_code == 403
                assert (await client.get(base + ROOT + "/approvals")).status_code == 403
            assert len(bodies) == 4  # Two diagnostics and exactly two actual turns.
            assert private not in json.dumps(bodies)
            assert "Shared synthetic reply" in json.dumps(bodies[-1]["messages"])
            assert not native._active_procs and not _INVOCATION_UNITS and not b.runner._managed_slots
            assert not list(config.rglob("*.jsonl")) and native._last_was_error
            observer.assert_not_awaited()
            b.runtime._context_injector.inject.assert_not_awaited()
            rows = await (await b.db.execute("SELECT * FROM pending_outreach")).fetchall()
            assert len(rows) == 2
            assert all("Public question" not in row["message"] and private not in row["message"] for row in rows)
            config_out = OutreachConfig(quiet_hours=QUIET_HOURS_DISABLED,
                channel_preferences={"default": "telegram"}, thresholds={"notification": 0},
                max_daily=999, surplus_daily=999, content_daily=999, notification_daily=999,
                morning_report_time="07:00", engagement_timeout_hours=24, engagement_poll_minutes=60)
            owner = SimpleNamespace(send_message=AsyncMock(return_value="fixture-owner-delivery"))
            pipeline = OutreachPipeline(GovernanceGate(config_out, b.db), AsyncMock(), ContentFormatter(),
                {"telegram": owner}, db=b.db, config=config_out, recipients={"telegram": "synthetic-owner"})
            scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config_out, b.db)
            scheduler._record_job_result = AsyncMock()
            await scheduler._drain_pending_job()
            assert owner.send_message.await_count == 2
            # Owner chooses a NEW normal workflow. Never upgrade the external session.
            b.runner._invoker = fake
            monkeypatch.setattr(b.runner, "_build_invocation", normal_build)
            original = fake.run_streaming.return_value
            fake.run_streaming.return_value = replace(original, text="Private owner result")
            trusted = await b.runner.spawn(DirectSessionRequest(prompt="Owner chooses a separate action",
                system_prompt="Synthetic trusted owner identity", notify=False))
            active = b.runner._active.get(trusted)
            if active:
                await asyncio.wait_for(asyncio.shield(active), 5)
            assert trusted not in {task.id, follow.id} and fake.run_streaming.call_args.args[0].peer_segment is None
            with pytest.raises(ConversationRefusal):
                await b.service.get(b.identities["alpha"], trusted)
            assert (await (await b.db.execute("SELECT count(*) FROM cc_sessions WHERE source_tag='agent_api'")).fetchone())[0] == 2
        finally:
            provider.shutdown()
            thread.join(5)
