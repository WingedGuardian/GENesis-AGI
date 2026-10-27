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
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
from fastmcp import FastMCP

mcp = FastMCP("genesis-peer")

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}
POLL_S = 10
RETRY_S = 3
SEND_ATTEMPTS = 3
# One deadline per tool call, covering sends and polling; with the 30 s request
# timeout and the final result fetch, a call ends well inside a 10-minute
# harness tool timeout.
MAX_WAIT_S = 480
RETRY_SAME_ID = (
    "Call genesis_ask again with this message_id and the same text and options: "
    "an exact retry never duplicates work."
)


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


@dataclass
class _Reply:
    status: int | None  # None: the request or its body was lost
    data: object = None  # the parsed JSON body, when it was JSON
    text: str = ""
    lost: str = ""  # why it was lost


async def _request(http: httpx.AsyncClient, method: str, path: str, **kwargs) -> _Reply:
    """Every HTTP call goes through here, and it never raises.

    A request whose response is lost (connection, timeout, a body that cannot be
    read) may still have taken effect on the server, so callers keep whatever
    ids they hold and report the loss instead of failing the tool call.
    """
    try:
        resp = await http.request(method, path, **kwargs)
        text = resp.text
    except httpx.RequestError as exc:
        return _Reply(None, lost=f"{type(exc).__name__}: {exc}"[:300])
    try:
        data = resp.json()
    except ValueError:
        data = None
    return _Reply(resp.status_code, data, text)


def _error(reply: _Reply) -> dict:
    if reply.status is None:
        return {"error": "transport", "detail": reply.lost}
    body = reply.data if reply.data is not None else reply.text[:500]
    return {"status": reply.status, "error": body}


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


def _deadline(wait_s: int) -> float:
    return time.monotonic() + min(max(0, wait_s), MAX_WAIT_S)


async def _poll(http: httpx.AsyncClient, task_id: str, deadline: float) -> dict:
    while True:
        r = await _request(http, "GET", f"/tasks/{task_id}")
        if r.status == 200 and isinstance(r.data, dict):
            summary = _summary(r.data)
            if summary["state"] in TERMINAL:
                return await _with_full_result(http, summary)
            hint = "Still running. Call genesis_check with this task_id to keep waiting."
        elif r.status == 404:
            # Admission may not have committed yet, or it was refused.
            summary = {"task_id": task_id, "state": "NOT_YET_KNOWN"}
            hint = (
                "Genesis has no record of this task yet. If this persists, the send was "
                "refused or the id is wrong. " + RETRY_SAME_ID
            )
        elif r.status in (None, 200, 503):
            # Lost, unreadable or briefly unavailable: the task itself is unaffected.
            summary = {"task_id": task_id, "state": "UNAVAILABLE"}
            hint = "Genesis could not be reached just now. Call genesis_check again shortly."
        else:
            return {"task_id": task_id, **_error(r)}
        if time.monotonic() >= deadline:
            return _add_note(summary, hint)
        await asyncio.sleep(POLL_S)


async def _with_full_result(http: httpx.AsyncClient, summary: dict) -> dict:
    # A failed or canceled task can also have a committed report worth reading.
    r = await _request(http, "GET", f"/tasks/{summary['task_id']}/result")
    if r.status == 200:
        summary["reply"] = r.text
        summary["reply_is_summary"] = False
    elif summary.get("reply") and summary["state"] == "TASK_STATE_COMPLETED":
        _add_note(summary, "The full reply is unavailable; the reply shown is a shortened head.")
    return summary


@mcp.tool
async def genesis_ask(
    message: str,
    context_id: str | None = None,
    profile: Literal["observe", "interact"] = "observe",
    effort: Literal["low", "medium", "high"] | None = None,
    in_reply_to: str | None = None,
    message_id: str | None = None,
    wait_s: int = 300,
) -> dict:
    """Hand Genesis a task or question and wait up to wait_s seconds (max 480) for the answer.

    Each call runs a full Genesis background session (minutes, not seconds) on
    the owner's shared model limits, and at most two run at once across every
    connected agent. Use it for what this session cannot do itself (messaging,
    Genesis's memory, its other tools), not for quick lookups.
    Genesis starts with NO knowledge of this session: write a self-contained
    brief (goal, relevant facts, file contents it needs, what done looks like).
    Pass context_id from an earlier answer to continue that conversation.
    profile: "observe" (default: read-only, answers questions, cheaper model)
    or "interact" (can act: send messages, file reports, browse; runs on the
    largest model). Choose interact only when Genesis must DO something, and
    name the exact destination (which server and channel, which repo, which
    person): Genesis can act only where it has access.
    in_reply_to: the message_id of a genesis_inbox message this answers; it
    needs that message's context_id too.
    message_id: pass the message_id an earlier call returned to retry that
    exact send (same text and options); it never duplicates work. Omit it for
    a new request. If the answer is not ready in wait_s, call genesis_check.
    """
    if in_reply_to and not context_id:
        return {"error": "in_reply_to needs the context_id of the message being answered."}
    deadline = _deadline(wait_s)
    meta: dict = {"profile": profile}
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
        # A 503 or a lost response may still have committed the send: retry the
        # SAME messageId, never a new one, and keep any receipt id a 503 named.
        known_task = None
        for attempt in range(SEND_ATTEMPTS):
            if attempt:
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(RETRY_S * attempt)
            r = await _request(http, "POST", "/message:send", json=body)
            if r.status == 503 and isinstance(r.data, dict) and r.data.get("taskId"):
                known_task = r.data["taskId"]
            if r.status not in (None, 503):
                break
        task = r.data.get("task", r.data) if isinstance(r.data, dict) else None
        if r.status == 200 and isinstance(task, dict) and task.get("id"):
            out = await _poll(http, task["id"], deadline)
        elif r.status in (None, 503, 200) and known_task:
            out = await _poll(http, known_task, deadline)
        elif r.status in (None, 503, 200):
            # Lost, still unavailable, or an answer that is not Genesis's: the
            # send may or may not have been admitted.
            out = _add_note(
                _error(r), "The send may or may not have reached Genesis. " + RETRY_SAME_ID
            )
        else:
            out = _error(r)
        out["message_id"] = message_id
        return out


@mcp.tool
async def genesis_check(task_id: str, wait_s: int = 300) -> dict:
    """Wait up to wait_s seconds (max 480) more for a task from genesis_ask, then report it."""
    async with _client() as http:
        return await _poll(http, task_id, _deadline(wait_s))


@mcp.tool
async def genesis_tasks(
    context_id: str | None = None, page_size: int = 10, page_token: str | None = None
) -> dict:
    """List this peer's recent Genesis tasks, newest first, optionally for one conversation.

    Pass next_page_token from a previous call as page_token (with the same
    context_id) for the next page.
    """
    params: dict = {"pageSize": max(1, min(page_size, 100))}
    if context_id:
        params["contextId"] = context_id
    if page_token:
        params["pageToken"] = page_token
    async with _client() as http:
        r = await _request(http, "GET", "/tasks", params=params)
        if r.status != 200 or not isinstance(r.data, dict):
            return _error(r)
        return {
            "tasks": [_summary(t) for t in r.data.get("tasks", [])],
            "total": r.data.get("totalSize"),
            "next_page_token": r.data.get("nextPageToken") or None,
        }


@mcp.tool
async def genesis_cancel(task_id: str) -> dict:
    """Cancel a running Genesis task."""
    async with _client() as http:
        r = await _request(http, "POST", f"/tasks/{task_id}:cancel", json={})
        if r.status == 200 and isinstance(r.data, dict):
            return _summary(r.data)
        return _add_note(
            {"task_id": task_id, **_error(r)},
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
            r = await _request(http, "POST", f"/outbox/{message_id}:ack", json={})
            (acked if r.status == 200 else ack_failed).append(message_id)
        params: dict = {"pageSize": 20}
        if page_token:
            params["pageToken"] = page_token
        r = await _request(http, "GET", "/outbox", params=params)
        if r.status != 200 or not isinstance(r.data, dict):
            return {**_error(r), "acked": acked, "ack_failed": ack_failed}
        messages = []
        for item in r.data.get("messages", []):
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
            "next_page_token": r.data.get("nextPageToken") or None,
        }
        if ack:
            out.update(acked=acked, ack_failed=ack_failed)
        return out


@mcp.tool
async def genesis_health() -> dict:
    """Check that Genesis's peer API is reachable and this peer's credential works."""
    async with _client() as http:
        r = await _request(http, "GET", "/health")
        return r.data if r.status == 200 and isinstance(r.data, dict) else _error(r)


def _suppress_docket_worker(server: FastMCP) -> bool:
    """Stop fastmcp 2.x starting an idle task-queue worker this server never uses.

    Same gate as Genesis's shared MCP launcher: without it fastmcp 2.14 runs a
    fakeredis worker that polls at 100 Hz for a task queue no tool here uses
    (every tool is task mode "forbidden"). The attribute is private, so it is set
    only when it exists. Delete this when the dependency floor reaches fastmcp 3,
    where docket is no longer a default (prefecthq/fastmcp#2887).
    """
    if not hasattr(server, "_is_mounted"):
        return False
    server._is_mounted = True
    return True


if __name__ == "__main__":
    if not _suppress_docket_worker(mcp):
        # stdout is the MCP channel; say it on stderr so version drift is visible.
        print(
            "genesis-peer: this fastmcp has no _is_mounted; its idle task-queue "
            "worker, if any, is not suppressed",
            file=sys.stderr,
        )
    mcp.run()
