"""Pinned A2A1.0 messages, with Genesis's intentionally bounded input policy."""

from __future__ import annotations

import json
import math

from a2a.types import CancelTaskRequest, Role, SendMessageRequest, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError, VersionNotSupportedError
from a2a.utils.proto_utils import validate_proto_required_fields
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.timestamp_pb2 import Timestamp
from packaging.version import InvalidVersion, Version


def validate_version(header: str | None) -> None:
    # The pinned SDK defaults a missing header to0.3 and accepts the major.
    # Avoid its logging decorator: the supplied header may contain private text.
    try:
        major = Version(header or "0.3").major
    except InvalidVersion:
        raise VersionNotSupportedError("A2A version is not supported") from None
    if major != 1:
        raise VersionNotSupportedError("A2A version is not supported")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite JSON value")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON value")
    return result


def parse_cancel(body: bytes, task_id: str) -> None:
    try:
        decoded = json.loads(
            body or b"{}",
            object_pairs_hook=_unique_object,
            parse_constant=_nonfinite,
            parse_float=_finite_float,
        )
        if not isinstance(decoded, dict):
            raise ValueError("object required")
        params = ParseDict(decoded, CancelTaskRequest(), ignore_unknown_fields=False)
        if params.id and params.id != task_id:
            raise ValueError("task identifier mismatch")
    except Exception:
        raise InvalidParamsError("Invalid cancellation request") from None
    if params.tenant:
        raise UnsupportedOperationError("Requested operation is unavailable")


def parse_send(body: bytes) -> tuple[dict, bool]:
    try:
        decoded = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_nonfinite,
            parse_float=_finite_float,
        )
        if not isinstance(decoded, dict):
            raise ValueError("object required")
        params = ParseDict(decoded, SendMessageRequest(), ignore_unknown_fields=False)
        validate_proto_required_fields(params)
        message_dict = MessageToDict(params.message)
    except Exception:
        # SDK ParseError/validation details can contain raw supplied values.
        raise InvalidParamsError("Invalid peer message") from None
    config = params.configuration
    msg = params.message
    if params.tenant or config.HasField("task_push_notification_config") or msg.extensions:
        raise UnsupportedOperationError("Requested operation is unavailable")
    if config.accepted_output_modes and any(
        mode != "text/plain" for mode in config.accepted_output_modes
    ):
        raise UnsupportedOperationError("Requested output mode is unavailable")
    if (
        msg.role != Role.ROLE_USER
        or not 1 <= len(msg.message_id) <= 128
        or not msg.message_id.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in msg.message_id)
        or msg.task_id
        or msg.reference_task_ids
        or config.history_length < 0
        or len(msg.parts) > 32
    ):
        raise InvalidParamsError("Invalid peer message")
    for part in msg.parts:
        kind = part.WhichOneof("content")
        if kind not in {"text", "data"} or (kind == "text" and not part.text.strip()):
            raise InvalidParamsError("Unsupported peer content")
        if part.filename or part.metadata:
            raise InvalidParamsError("Unsupported peer content metadata")
        if part.media_type and part.media_type not in {"text/plain", "application/json"}:
            raise InvalidParamsError("Unsupported peer media type")
    return message_dict, config.return_immediately


STATES = {
    "submitted": TaskState.TASK_STATE_SUBMITTED,
    "working": TaskState.TASK_STATE_WORKING,
    "input_required": TaskState.TASK_STATE_INPUT_REQUIRED,
    "completed": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "canceled": TaskState.TASK_STATE_CANCELED,
    "rejected": TaskState.TASK_STATE_REJECTED,
}


def task_view(row: dict) -> Task:
    status = TaskStatus(state=STATES[row["state"]])
    if row.get("updated_at"):
        stamp = Timestamp()
        stamp.FromJsonString(row["updated_at"])
        status.timestamp.CopyFrom(stamp)
    return Task(id=row["id"], context_id=row["context_id"], status=status)
