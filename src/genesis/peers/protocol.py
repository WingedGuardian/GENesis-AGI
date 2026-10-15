"""Pinned A2A1.0 messages, with Genesis's intentionally bounded input policy."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from a2a.types import CancelTaskRequest, Role, SendMessageRequest
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError, VersionNotSupportedError
from a2a.utils.proto_utils import validate_proto_required_fields
from google.protobuf.json_format import MessageToDict, ParseDict
from packaging.version import InvalidVersion, Version

from genesis.cc.types import CCModel, EffortLevel


def validate_version(header: str | None) -> None:
    # A missing header means 0.3. The spec (3.6.2) requires matching Major.Minor
    # and ignoring only the patch; the pinned SDK checks the major alone.
    # Avoid its logging decorator: the supplied header may contain private text.
    try:
        version = Version(header or "0.3")
    except InvalidVersion:
        raise VersionNotSupportedError("A2A version is not supported") from None
    plain = version.epoch == 0 and str(version) == version.base_version
    if not plain or (version.release + (0, 0))[:2] != (1, 0):
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


@dataclass(frozen=True)
class Incoming:
    message: dict
    prompt: str
    options: dict
    canonical: str


def parse_send(body: bytes) -> Incoming:
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
    if (
        params.tenant
        or config.HasField("task_push_notification_config")
        or msg.extensions
        or msg.metadata
        or not config.return_immediately
    ):
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
    options = decoded.get("metadata", {})
    try:
        if not isinstance(options, dict) or set(options) - {
            "profile",
            "model",
            "effort",
            "timeout_s",
        }:
            raise ValueError
        if options.get("profile", "observe") not in {"observe", "interact"}:
            raise ValueError
        CCModel(options.get("model", "sonnet"))
        EffortLevel(options.get("effort", "medium"))
        timeout = options.get("timeout_s", 3600)
        # Protobuf Struct numbers serialize as doubles in the pinned SDK.
        # Accept integral numeric values, then pass an actual int to native CC.
        if type(timeout) not in (int, float) or not 1 <= timeout <= 7200 or timeout != int(timeout):
            raise ValueError
        if "timeout_s" in options:
            options["timeout_s"] = int(timeout)
        if options.get("effort", "medium") not in {"low", "medium", "high"}:
            raise ValueError
        prompt = "\n\n".join(
            part["text"] if "text" in part else json.dumps(part["data"], ensure_ascii=False)
            for part in message_dict["parts"]
        )
        if len(prompt.encode("utf-8")) > 256 * 1024:
            raise ValueError
        canonical = json.dumps(
            MessageToDict(params), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except Exception:
        raise InvalidParamsError("Invalid conversation options") from None
    return Incoming(message_dict, prompt, options, canonical)
