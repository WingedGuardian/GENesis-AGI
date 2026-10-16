"""Owned result projection; never return raw metadata or a stored file path."""

import hashlib
import os
import stat
from pathlib import Path

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from genesis.cc.managed_session import metadata
from genesis.peers.disclosure_scan import json_strings_safe

MAX_RESULT = 2 * 1024 * 1024
STATES = {
    "pending": TaskState.TASK_STATE_SUBMITTED,
    "running": TaskState.TASK_STATE_WORKING,
    "cancelling": TaskState.TASK_STATE_WORKING,
    "succeeded": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "cancelled": TaskState.TASK_STATE_CANCELED,
}


def full_result(row):
    from genesis.peers.conversation import ConversationRefusal, uuid_id

    meta = metadata(row)
    if row["status"] == "active":
        raise ConversationRefusal("result_not_ready", 409)
    if not meta.get("result_committed") or meta.get("result_partial"):
        raise ConversationRefusal("result_unavailable", 409)
    sid = uuid_id(row["id"])
    path = Path.home() / ".genesis" / "output" / f"bg-session-{sid}.md"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT:
                raise ValueError
            raw = file.read(MAX_RESULT + 1)
            if len(raw) > MAX_RESULT:
                raise ValueError
        # Only the bytes finalization committed are served: a later session
        # may have rewritten the file in its shared output directory.
        if hashlib.sha256(raw).hexdigest() != meta.get("result_sha256"):
            raise ValueError
        text = raw.decode("utf-8")
    except (OSError, ValueError):
        # The saved metadata head is deliberately lossy; never label it full.
        raise ConversationRefusal("result_unavailable", 409) from None
    if not json_strings_safe(text):
        raise ConversationRefusal("disclosure_refused", 409)
    return text


def project(row, pending_approvals=False):
    from genesis.peers.conversation import head

    meta = metadata(row)
    state = meta.get("native_state", "pending")
    if row["status"] == "active" and state in {"succeeded", "failed", "cancelled"}:
        state = "running"  # Final metadata can precede the native status commit.
    status = TaskStatus(state=STATES.get(state, TaskState.TASK_STATE_WORKING))
    stamp = row.get("completed_at") or row.get("last_activity_at") or row.get("started_at")
    if stamp:
        ts = Timestamp()
        ts.FromJsonString(stamp)
        status.timestamp.CopyFrom(ts)
    view = {
        "nativeState": state,
        "pendingOwnerActions": bool(pending_approvals),
        "cleanupUnconfirmed": bool(meta.get("cleanup_unconfirmed")),
        "resultCommitted": bool(meta.get("result_committed")),
        "resultPartial": bool(meta.get("result_partial")),
    }
    if pending_approvals and row["status"] == "active":
        status.state = TaskState.TASK_STATE_AUTH_REQUIRED
    if row["status"] != "active":
        text = meta.get("output_text") or ""
        if json_strings_safe(text):
            if text:
                status.message.CopyFrom(
                    Message(
                        message_id=row["id"] + ":reply",
                        context_id=meta["context_id"],
                        task_id=row["id"],
                        role=Role.ROLE_AGENT,
                        parts=[Part(text=head(text))],
                    )
                )
                view["replyIsSummary"] = True  # Metadata is a stored, lossy head.
        else:
            view["replyError"] = "disclosure_refused"
    tools = meta.get("tools_summary", {})
    view["toolsSummary"] = tools if json_strings_safe(tools) else {}
    view["toolsSummaryIsLowerBound"] = (
        bool(meta.get("stream_lines_dropped"))
        or sum(value for value in tools.values() if type(value) is int) >= 100
    )
    if state == "failed":
        view["error"] = (
            meta.get("error")
            if meta.get("error")
            in {
                "execution_failed",
                "deadline_exceeded",
                "result_unavailable",
                "not_started",
                "outcome_unknown",
            }
            else "execution_failed"
        )
    return MessageToDict(
        Task(id=row["id"], context_id=meta["context_id"], status=status, metadata=view)
    )
