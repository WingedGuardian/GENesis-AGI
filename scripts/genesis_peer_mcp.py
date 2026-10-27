# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp>=2.0,<3.0", "httpx"]
# ///
"""Same-owner A2A client for Genesis, as a stdio MCP server.

Runs on any machine an enrolled peer agent lives on (Claude Code, Codex,
OpenCode); it needs nothing from a Genesis checkout:

    uv run --script genesis_peer_mcp.py

Configuration comes from the environment:

    GENESIS_PEER_URL    the peer API base, e.g. https://HOST/v1/agent/a2a
    GENESIS_PEER_TOKEN  the bearer issued for this peer at enrollment

Contract: docs/reference/agent-conversations.md (send, poll, result) and
docs/reference/agent-outbox.md (messages Genesis starts). Genesis runs each
turn as its own background session; this client only sends and reads.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
from fastmcp import FastMCP

mcp = FastMCP("genesis-peer")

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}
POLL_S = 10
RETRY_S = 3
SEND_ATTEMPTS = 3
# Keep every call well inside a 10-minute harness tool timeout.
MAX_WAIT_S = 480


def _client() -> httpx.AsyncClient:
    url = os.environ.get("GENESIS_PEER_URL", "").rstrip("/")
    token = os.environ.get("GENESIS_PEER_TOKEN", "")
    if not url or not token:
        raise RuntimeError(
            "Set GENESIS_PEER_URL and GENESIS_PEER_TOKEN for the genesis-peer MCP server"
        )
    return httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        timeout=30,
    )


def _error(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    return {"status": resp.status_code, "error": body}


def _summary(task: dict) -> dict:
    status = task.get("status", {})
    meta = task.get("metadata", {})
    reply = "".join(p.get("text", "") for p in status.get("message", {}).get("parts", []))
    out = {
        "task_id": task.get("id"),
        "context_id": task.get("contextId"),
        "state": status.get("state"),
        "updated": status.get("timestamp"),
    }
    if reply:
        out["reply"] = reply
        out["reply_is_summary"] = bool(meta.get("replyIsSummary"))
    for key in ("pendingOwnerActions", "replyError", "error"):
        if meta.get(key):
            out[key] = meta[key]
    notes = []
    if meta.get("replyError") == "disclosure_refused":
        notes.append(
            "Genesis finished but withheld the reply because part of it looked like a "
            "credential. Ask again for the answer without keys, tokens or secrets."
        )
    if meta.get("pendingOwnerActions"):
        notes.append("Waiting on the owner's approval in Genesis (Telegram or dashboard).")
    if notes:
        out["note"] = " ".join(notes)
    return out


def _add_note(out: dict, text: str) -> dict:
    out["note"] = f"{out['note']} {text}" if out.get("note") else text
    return out


async def _poll(http: httpx.AsyncClient, task_id: str, wait_s: int) -> dict:
    deadline = time.monotonic() + min(max(0, wait_s), MAX_WAIT_S)
    while True:
        resp = await http.get(f"/tasks/{task_id}")
        if resp.status_code == 200:
            summary = _summary(resp.json())
            if summary["state"] in TERMINAL:
                return await _with_full_result(http, summary)
            hint = "Still running. Call genesis_check with this task_id to keep waiting."
        elif resp.status_code == 404:
            # Admission may not have committed yet, or it was refused.
            summary = {"task_id": task_id, "state": "NOT_YET_KNOWN"}
            hint = (
                "Genesis has no record of this task yet. If this persists, the send was "
                "refused or the id is wrong: call genesis_ask again with the same "
                "message_id (an exact retry never duplicates work)."
            )
        elif resp.status_code == 503:
            summary = {"task_id": task_id, "state": "UNAVAILABLE"}
            hint = "Genesis is briefly unavailable. Call genesis_check again shortly."
        else:
            return {"task_id": task_id, **_error(resp)}
        if time.monotonic() >= deadline:
            return _add_note(summary, hint)
        await asyncio.sleep(POLL_S)


async def _with_full_result(http: httpx.AsyncClient, summary: dict) -> dict:
    if summary["state"] != "TASK_STATE_COMPLETED":
        return summary
    resp = await http.get(f"/tasks/{summary['task_id']}/result")
    if resp.status_code == 200:
        summary["reply"] = resp.text
        summary["reply_is_summary"] = False
    elif summary.get("reply"):
        _add_note(summary, "The full reply is unavailable; the reply shown is a shortened head.")
    return summary


@mcp.tool
async def genesis_ask(
    message: str,
    context_id: str | None = None,
    profile: str | None = None,
    effort: str | None = None,
    in_reply_to: str | None = None,
    message_id: str | None = None,
    wait_s: int = 300,
) -> dict:
    """Send Genesis a task or question and wait up to wait_s seconds (max 480) for the answer.

    Genesis starts with NO knowledge of this session: write a self-contained
    brief (goal, relevant facts, file contents it needs, what done looks like).
    Pass context_id from an earlier answer to continue that conversation.
    profile: "interact" (can act: messaging, browsing, memory) or "observe"
    (read-only); omitted, Genesis picks from its autonomy level.
    effort: "low" | "medium" | "high".
    in_reply_to: the message_id of a genesis_inbox message this answers; it
    needs that message's context_id too.
    message_id: pass the message_id an earlier call returned to retry that
    exact send (same text and options); it never duplicates work. Omit it for
    a new request. If the answer is not ready in wait_s, call genesis_check.
    """
    if in_reply_to and not context_id:
        return {"error": "in_reply_to needs the context_id of the message being answered."}
    meta: dict = {}
    if profile:
        meta["profile"] = profile
    if effort:
        meta["effort"] = effort
    if in_reply_to:
        meta["inReplyTo"] = in_reply_to
    if not message_id:
        # A reply's id is stable so a retried reply is the same reply.
        message_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"genesis-peer-reply:{in_reply_to}")
            if in_reply_to
            else uuid.uuid4()
        )
    msg: dict = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": message}]}
    if context_id:
        msg["contextId"] = context_id
    body = {"message": msg, "configuration": {"returnImmediately": True}, "metadata": meta}
    async with _client() as http:
        # A 503 send may still commit: retry the SAME messageId, never a new one.
        for attempt in range(SEND_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_S * attempt)
            resp = await http.post("/message:send", json=body)
            if resp.status_code != 503:
                break
        if resp.status_code == 200:
            data = resp.json()
            out = await _poll(http, data.get("task", data)["id"], wait_s)
        else:
            out = _error(resp)
            err = out["error"]
            task_id = err.get("taskId") if isinstance(err, dict) else None
            if resp.status_code == 503 and task_id:
                out = await _poll(http, task_id, wait_s)
        out["message_id"] = message_id
        return out


@mcp.tool
async def genesis_check(task_id: str, wait_s: int = 300) -> dict:
    """Wait up to wait_s seconds (max 480) more for a task from genesis_ask, then report it."""
    async with _client() as http:
        return await _poll(http, task_id, wait_s)


@mcp.tool
async def genesis_tasks(context_id: str | None = None, page_size: int = 10) -> dict:
    """List this peer's recent Genesis tasks, newest first, optionally for one conversation."""
    params: dict = {"pageSize": max(1, min(page_size, 100))}
    if context_id:
        params["contextId"] = context_id
    async with _client() as http:
        resp = await http.get("/tasks", params=params)
        if resp.status_code != 200:
            return _error(resp)
        data = resp.json()
        return {
            "tasks": [_summary(t) for t in data.get("tasks", [])],
            "total": data.get("totalSize"),
            "next_page_token": data.get("nextPageToken") or None,
        }


@mcp.tool
async def genesis_cancel(task_id: str) -> dict:
    """Cancel a running Genesis task."""
    async with _client() as http:
        resp = await http.post(f"/tasks/{task_id}:cancel", json={})
        if resp.status_code == 200:
            return _summary(resp.json())
        return _add_note(
            {"task_id": task_id, **_error(resp)},
            "Check the task with genesis_check; a cancellation may still be in progress.",
        )


@mcp.tool
async def genesis_inbox(ack: list[str] | None = None, page_token: str | None = None) -> dict:
    """Read messages Genesis started for this peer (its outbox), oldest first.

    Nothing is marked collected by reading. Once you have dealt with messages
    (answered them, or decided not to), pass their message_ids as ack on a
    later call so they stop being shown; an unacknowledged message is shown
    again, which is the safe direction. Reply with genesis_ask, passing the
    message's context_id and its message_id as in_reply_to.
    """
    async with _client() as http:
        acked, ack_failed = [], []
        for message_id in ack or []:
            resp = await http.post(f"/outbox/{message_id}:ack", json={})
            (acked if resp.status_code == 200 else ack_failed).append(message_id)
        params: dict = {"pageSize": 20}
        if page_token:
            params["pageToken"] = page_token
        resp = await http.get("/outbox", params=params)
        if resp.status_code != 200:
            return {**_error(resp), "acked": acked, "ack_failed": ack_failed}
        data = resp.json()
        messages = []
        for item in data.get("messages", []):
            m = item.get("message", {})
            messages.append(
                {
                    "message_id": m.get("messageId"),
                    "context_id": m.get("contextId"),
                    "created": item.get("createdAt"),
                    "text": "".join(p.get("text", "") for p in m.get("parts", [])),
                }
            )
        out = {
            "messages": messages,
            "next_page_token": data.get("nextPageToken") or None,
        }
        if ack:
            out.update(acked=acked, ack_failed=ack_failed)
        return out


@mcp.tool
async def genesis_health() -> dict:
    """Check that Genesis's peer API is reachable and this peer's credential works."""
    async with _client() as http:
        resp = await http.get("/health")
        return resp.json() if resp.status_code == 200 else _error(resp)


if __name__ == "__main__":
    mcp.run()
