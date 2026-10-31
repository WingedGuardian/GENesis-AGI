"""The standalone same-owner A2A client (scripts/genesis_peer_mcp.py).

Driven against an in-process httpx MockTransport that speaks the documented
peer contract (docs/reference/agent-conversations.md and agent-outbox.md), so
no server runs.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "genesis_peer_mcp.py"


@pytest.fixture
def peer(monkeypatch):
    spec = importlib.util.spec_from_file_location("genesis_peer_mcp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, mod)
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
    assert "this message_id" in out["note"]


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


async def test_lost_send_responses_keep_the_retry_id(peer, monkeypatch):
    def handler(request, n):
        raise httpx.ConnectError("connection reset")

    calls = _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("send the email", wait_s=5)

    sends = _sends(calls)
    assert len(sends) == peer.SEND_ATTEMPTS
    assert {s["message"]["messageId"] for s in sends} == {out["message_id"]}
    assert out["error"] == "transport"
    assert "this message_id" in out["note"]


async def test_one_lost_send_response_is_retried_with_the_same_id(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            if n == 1:
                raise httpx.ReadTimeout("lost")
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, text="done")
        return httpx.Response(200, json=_task("TASK_STATE_COMPLETED"))

    calls = _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=5)

    first, second = _sends(calls)
    assert first["message"]["messageId"] == second["message"]["messageId"]
    assert out["state"] == "TASK_STATE_COMPLETED"
    assert out["message_id"] == first["message"]["messageId"]


async def test_lost_poll_keeps_the_task_and_message_ids(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        raise httpx.ConnectError("down")

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=0)

    assert out["task_id"] == "task-1"
    assert out["state"] == "UNAVAILABLE"
    assert out["message_id"]


async def test_failed_task_still_returns_its_committed_report(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/result"):
            return httpx.Response(200, text="full failure report")
        return httpx.Response(200, json=_task("TASK_STATE_FAILED", error="execution_failed"))

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=0)

    assert out["state"] == "TASK_STATE_FAILED"
    assert out["reply"] == "full failure report"


async def test_task_listing_forwards_the_page_token(peer, monkeypatch):
    page = {"tasks": [_task("TASK_STATE_COMPLETED")], "totalSize": 11, "nextPageToken": "p2"}
    calls = _bind(peer, monkeypatch, lambda request, n: httpx.Response(200, json=page))
    out = await _fn(peer.genesis_tasks)(context_id="ctx-1", page_token="p2")

    assert calls[0].url.params["pageToken"] == "p2"
    assert calls[0].url.params["contextId"] == "ctx-1"
    assert out["next_page_token"] == "p2"


async def test_ask_defaults_to_the_read_only_profile(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        return httpx.Response(200, json=_task("TASK_STATE_WORKING"))

    calls = _bind(peer, monkeypatch, handler)
    await _fn(peer.genesis_ask)("what did we decide about saves?", wait_s=0)

    assert _sends(calls)[0]["metadata"] == {"profile": "observe"}


async def test_explicit_interact_is_forwarded(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            return httpx.Response(200, json={"task": _task("TASK_STATE_SUBMITTED")})
        return httpx.Response(200, json=_task("TASK_STATE_WORKING"))

    calls = _bind(peer, monkeypatch, handler)
    await _fn(peer.genesis_ask)("file this bug", profile="interact", wait_s=0)

    assert _sends(calls)[0]["metadata"]["profile"] == "interact"


async def test_lost_result_fetch_keeps_the_task_and_the_head_reply(peer, monkeypatch):
    head = _task("TASK_STATE_COMPLETED")
    head["status"]["message"] = {"parts": [{"text": "short head"}]}

    def handler(request, n):
        if request.url.path.endswith("/result"):
            raise httpx.ReadTimeout("lost")
        return httpx.Response(200, json=head)

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=0)

    assert out["task_id"] == "task-1"
    assert out["reply"] == "short head"
    assert "shortened head" in out["note"]


async def test_failed_task_without_a_report_gets_no_head_note(peer, monkeypatch):
    head = _task("TASK_STATE_FAILED", error="execution_failed")
    head["status"]["message"] = {"parts": [{"text": "partial"}]}

    def handler(request, n):
        if request.url.path.endswith("/result"):
            return httpx.Response(409, json={"code": "result_unavailable"})
        return httpx.Response(200, json=head)

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_check)("task-1", wait_s=0)

    assert out["state"] == "TASK_STATE_FAILED"
    assert "note" not in out


async def test_unreadable_send_reply_keeps_the_retry_id(peer, monkeypatch):
    def handler(request, n):
        return httpx.Response(200, text="<html>proxy error</html>")

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=0)

    assert out["message_id"]
    assert "this message_id" in out["note"]


async def test_a_receipt_named_by_an_earlier_503_survives_a_later_loss(peer, monkeypatch):
    def handler(request, n):
        if request.url.path.endswith("/message:send"):
            if n == 1:
                return httpx.Response(503, json={"code": "not_ready", "taskId": "task-1"})
            raise httpx.ConnectError("down")
        if request.url.path.endswith("/result"):
            return httpx.Response(200, text="done")
        return httpx.Response(200, json=_task("TASK_STATE_COMPLETED"))

    _bind(peer, monkeypatch, handler)
    out = await _fn(peer.genesis_ask)("hello", wait_s=5)

    assert out["task_id"] == "task-1"
    assert out["reply"] == "done"


def test_server_starts_without_the_obsolete_task_worker_patch():
    tree = ast.parse(SCRIPT.read_text())
    guard = next(
        n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)
    )
    texts = [ast.unparse(n) for n in guard.body]
    assert texts == ["mcp.run()"]


async def test_server_has_no_task_worker_and_keeps_its_tools(peer):
    from fastmcp import Client

    tools = await peer.mcp.list_tools()
    assert tools
    assert await peer.mcp.get_tasks() == []
    async with Client(peer.mcp) as client:
        assert peer.mcp._docket is None
        assert peer.mcp._worker is None
        assert len(await client.list_tools()) == len(tools)
