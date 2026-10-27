"""The standalone same-owner A2A client (scripts/genesis_peer_mcp.py).

Driven against an in-process httpx MockTransport that speaks the documented
peer contract (docs/reference/agent-conversations.md and agent-outbox.md), so
no server runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "genesis_peer_mcp.py"


@pytest.fixture
def peer(monkeypatch):
    spec = importlib.util.spec_from_file_location("genesis_peer_mcp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "POLL_S", 0)
    monkeypatch.setattr(mod, "RETRY_S", 0)
    return mod


def _bind(mod, monkeypatch, handler):
    calls = []

    def record(request):
        calls.append(request)
        return handler(request, len(calls))

    def client():
        return httpx.AsyncClient(
            base_url="https://peer.test/v1/agent/a2a",
            transport=httpx.MockTransport(record),
            headers={"Authorization": "Bearer t", "A2A-Version": "1.0"},
        )

    monkeypatch.setattr(mod, "_client", client)
    return calls


def _fn(tool):
    return getattr(tool, "fn", tool)


def _task(state, **meta):
    return {
        "id": "task-1",
        "contextId": "ctx-1",
        "status": {"state": state, "timestamp": "2026-10-26T00:00:00Z"},
        "metadata": meta,
    }


def _sends(calls):
    return [json.loads(c.content) for c in calls if c.url.path.endswith("/message:send")]


async def test_send_503_is_retried_with_the_same_message_id_then_polled(peer, monkeypatch):
    def handler(request, n):
        path = request.url.path
        if path.endswith("/message:send"):
            if n == 1:
                return httpx.Response(503, json={"code": "not_ready", "taskId": "task-1"})
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        if path.endswith("/tasks/task-1"):
            return httpx.Response(200, json=_task("TASK_STATE_COMPLETED"))
        if path.endswith("/tasks/task-1/result"):
            return httpx.Response(200, text="full answer")
        return httpx.Response(404)

    calls = _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=5)

    sends = _sends(calls)
    assert len(sends) == 2
    assert sends[0]["message"]["messageId"] == sends[1]["message"]["messageId"]
    assert sends[0]["configuration"] == {"returnImmediately": True}
    assert out["state"] == "TASK_STATE_COMPLETED"
    assert out["reply"] == "full answer"
    assert out["reply_is_summary"] is False
    assert out["message_id"] == sends[0]["message"]["messageId"]


async def test_send_that_stays_503_reports_the_error_and_the_retry_id(peer, monkeypatch):
    def handler(request, n):
        return httpx.Response(503, json={"code": "not_ready"})

    calls = _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=5)

    sends = _sends(calls)
    assert len(sends) == peer.SEND_ATTEMPTS
    assert {s["message"]["messageId"] for s in sends} == {out["message_id"]}
    assert out["status"] == 503


async def test_task_unknown_at_the_deadline_points_at_a_same_id_retry(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            return httpx.Response(503, json={"code": "not_ready", "taskId": "task-1"})
        return httpx.Response(404, json={"code": "not_found"})

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=0)

    assert out["state"] == "NOT_YET_KNOWN"
    assert "same message_id" in out["note"]


async def test_transient_503_while_polling_keeps_waiting(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/result"):
            return httpx.Response(200, text="done")
        if n == 1:
            return httpx.Response(503, json={"code": "not_ready"})
        return httpx.Response(200, json=_task("TASK_STATE_COMPLETED"))

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=5)

    assert out["state"] == "TASK_STATE_COMPLETED"
    assert out["reply"] == "done"


async def test_withheld_reply_is_explained(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/result"):
            return httpx.Response(409, json={"code": "disclosure_refused"})
        return httpx.Response(
            200, json=_task("TASK_STATE_COMPLETED", replyError="disclosure_refused")
        )

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=0)

    assert out["replyError"] == "disclosure_refused"
    assert "withheld the reply" in out["note"]
    assert "reply" not in out


async def test_running_task_returns_a_resume_hint(peer, monkeypatch):
    def handler(request, n):
        return httpx.Response(200, json=_task("TASK_STATE_WORKING", pendingOwnerActions=True))

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=0)

    assert out["state"] == "TASK_STATE_WORKING"
    assert "owner's approval" in out["note"]
    assert "genesis_check" in out["note"]


async def test_reply_options_are_sent_and_a_reply_id_is_stable(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        return httpx.Response(200, json=_task("TASK_STATE_WORKING"))

    calls = _bind(peer, monkeypatch, handler)
    for _ in range(2):
        await _fn(peer.genesis_ask)(
            "answer",
            context_id="ctx-1",
            profile="observe",
            effort="low",
            in_reply_to="m-9",
            wait_s=0,
        )

    first, second = _sends(calls)
    assert first["metadata"] == {"profile": "observe", "effort": "low", "inReplyTo": "m-9"}
    assert first["message"]["contextId"] == "ctx-1"
    assert first["message"]["messageId"] == second["message"]["messageId"]


async def test_reply_without_context_is_refused_before_sending(peer, monkeypatch):
    calls = _bind(peer, monkeypatch, lambda request, n: httpx.Response(500))
    out = await _fn(peer.genesis_ask)("answer", in_reply_to="m-9")

    assert "context_id" in out["error"]
    assert calls == []


async def test_inbox_reads_without_acknowledging(peer, monkeypatch):
    page = {
        "messages": [
            {
                "message": {
                    "messageId": "m-1",
                    "contextId": "ctx-1",
                    "parts": [{"text": "hi"}],
                },
                "createdAt": "2026-10-26T00:00:00Z",
            }
        ],
        "nextPageToken": "next",
    }
    calls = _bind(peer, monkeypatch, lambda request, n: httpx.Response(200, json=page))
    out = await _fn(peer.genesis_inbox)()

    assert [c.method for c in calls] == ["GET"]
    assert out["messages"][0] == {
        "message_id": "m-1",
        "context_id": "ctx-1",
        "created": "2026-10-26T00:00:00Z",
        "text": "hi",
    }
    assert out["next_page_token"] == "next"


async def test_inbox_acknowledges_only_the_ids_passed_and_reports_failures(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("m-bad:ack"):
            return httpx.Response(404, json={"code": "not_found"})
        if request.method == "POST":
            return httpx.Response(200, json={"messageId": "m-1", "acknowledged": True})
        return httpx.Response(200, json={"messages": []})

    calls = _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_inbox)(ack=["m-1", "m-bad"])

    acks = [c.url.path for c in calls if c.method == "POST"]
    assert acks == ["/v1/agent/a2a/outbox/m-1:ack", "/v1/agent/a2a/outbox/m-bad:ack"]
    assert out["acked"] == ["m-1"]
    assert out["ack_failed"] == ["m-bad"]


def test_missing_configuration_names_both_variables(peer, monkeypatch):
    monkeypatch.delenv("GENESIS_PEER_URL", raising=False)
    monkeypatch.delenv("GENESIS_PEER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GENESIS_PEER_URL and GENESIS_PEER_TOKEN"):
        peer._client()
