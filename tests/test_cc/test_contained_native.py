"""Real CLI/scopes and native receipts, using only synthetic local providers."""

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from genesis.cc import fallback_state, peer_availability
from genesis.cc.exceptions import CCQuotaExhaustedError
from genesis.cc.invoker import CCInvoker
from genesis.cc.managed_fallback import record_fallback
from genesis.cc.peer_segment import _INVOCATION_UNITS
from genesis.cc.provider_readiness import READY
from genesis.db.crud import cc_sessions
from genesis.peers.results import full_result
from tests.test_cc.test_contained_managed import prepare, request
from tests.test_cc.test_managed_fallback import managed as managed


@pytest.mark.skipif(shutil.which("claude") is None, reason="native Claude CLI unavailable")
async def test_native_managed_failover_and_committed_result(managed, monkeypatch, tmp_path):
    b = managed
    config = tmp_path / "cli-config"
    config.mkdir()
    marker = "SYNTHETIC_PRIVATE_MANAGED_CONTEXT_829"
    (config / "CLAUDE.md").write_text(marker)
    attachment = tmp_path / "private-attachment.txt"
    attachment.write_text(marker)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    bodies, routes = [], []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "count_tokens" in self.path:
                payload, code = {"input_tokens": 10}, 200
            else:
                bodies.append(body)
                routes.append({"path": self.path, "ready": READY in json.dumps(body.get("messages", [])),
                    "task": "Public question" in json.dumps(body.get("messages", [])),
                    "private_seen": marker in json.dumps(body)})
                if "/primary/" in self.path:
                    payload = {"type": "error", "error": {
                        "type": "invalid_request_error", "message": "Insufficient credit",
                    }}
                    code = 400  # Non-retrying genuine provider refusal.
                else:
                    text = READY if READY in json.dumps(body["messages"]) else "Synthetic managed answer"
                    payload = {"id": "msg_fixture", "type": "message", "role": "assistant",
                        "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": text}],
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 2}}
                    code = 200
            content_type = "application/json"
            if code == 200 and body.get("stream") and "count_tokens" not in self.path:
                # Match the provider protocol: returning JSON to a streaming
                # request makes the CLI retry with a non-streaming request.
                message = {**payload, "content": [], "stop_reason": None}
                events = [
                    ("message_start", {"type": "message_start", "message": message}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                raw = "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events).encode()
                content_type = "text/event-stream"
            else:
                raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            b.config.clear()
            b.config.update(default="primary", models={name: {
                "anthropic_base_url": f"http://127.0.0.1:{server.server_port}/{name}",
                "model_id": "synthetic-" + name, "auth_env": "SYNTHETIC_PROVIDER_KEY",
                "failover_order": order,
            } for order, name in enumerate(("primary", "backup"))})
            observer = AsyncMock()
            real = CCInvoker(claude_path=shutil.which("claude"), on_cc_status_change=observer)
            failures = []
            run = real.run_streaming
            async def observed(invocation, on_event=None):
                try:
                    return await run(invocation, on_event=on_event)
                except BaseException as exc:
                    failures.append({"type": type(exc).__name__,
                        "credit_refusal": "insufficient credit" in str(exc).lower(),
                        "no_result": "without a result" in str(exc),
                        "authority_refused": "authority" in str(exc)})
                    raise
            monkeypatch.setattr(real, "run_streaming", observed)
            real._last_was_error = True
            b.runner._invoker = real
            b.runner._rt._cc_invoker = real
            def forbidden(*args, **kwargs):
                raise AssertionError("Private owner invocation builder reached")
            monkeypatch.setattr(b.runner, "_build_invocation", forbidden)
            fallback_state.enter("primary", "earlier", "rate_limit")
            await record_fallback(b.db, "earlier", "previous", success=True)
            peer_availability.note_failure("backup", CCQuotaExhaustedError("Synthetic old refusal"))
            before = peer_availability.read_peer("backup")
            from dataclasses import replace
            req = replace(request(True), prompt=f"Public question. Consider @{attachment}", timeout_s=45)
            sid = await prepare(b, req)
            await b.runner.adopt(sid, req)
            await asyncio.wait_for(asyncio.shield(b.runner._active[sid]), 50)
            row = await cc_sessions.get_by_id(b.db, sid)
            meta = json.loads(row["metadata"])
            assert row["status"] == "completed" and meta["native_state"] == "succeeded", {
                "failures": failures, "requests": len(bodies),
                "attempts": [(a["provider"], a["outcome"]) for a in meta.get("provider_attempts", [])],
                "error": meta.get("error"),
            }
            assert meta["execution_started"] and meta["execution_provider"] == "backup"
            assert [a["outcome"] for a in meta["provider_attempts"]] == ["refused", "ready"]
            assert full_result(row) == "Synthetic managed answer"
            assert meta["transcript_path"] == "" and meta["tools_summary"] == {}
            assert marker not in json.dumps(bodies)
            assert sum(route["task"] for route in routes) == 1
            assert sum(route["ready"] for route in routes) == 3
            assert len(bodies) == 4
            assert not list(config.rglob("*.jsonl"))
            assert not real._active_procs and not _INVOCATION_UNITS and not b.runner._managed_slots
            assert real._last_was_error and fallback_state.read().fallback == "earlier"
            assert peer_availability.read_peer("backup") == before
            observer.assert_not_awaited()
            b.alerts.assert_not_awaited()
            monitor = await (await b.db.execute(
                "SELECT provider_used FROM call_site_last_run WHERE call_site_id='cc_fallback'"
            )).fetchone()
            assert monitor[0] == "earlier"
            from genesis.memory.extraction_job import _find_extractable_sessions
            assert await _find_extractable_sessions(b.db, transcript_dir=config / "projects") == []
        finally:
            server.shutdown()
            thread.join(5)
