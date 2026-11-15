"""Harmless elicitation qualification helpers; never production action authority."""

import asyncio
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path

SCHEMA = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean", "title": "Confirm this fixture action"}},
    "required": ["confirmed"],
}
APPROVAL_META = {
    "codex_approval_kind": "mcp_tool_call",
    "codex_request_type": "approval_request",
    "codex_requires_user_input": True,
    "tool_name": "record",
    "tool_params": {"target": "approval-sink.jsonl"},
}


def accepted(response):
    """Whether an explicit response meets the fixture's one-action contract."""
    if not isinstance(response, dict) or set(response) - {"action", "content", "_meta"}:
        return False
    content = response.get("content")
    return (
        response.get("action") == "accept"
        and response.get("_meta") in (None, {})
        and isinstance(content, dict)
        and set(content) == {"confirmed"}
        and type(content["confirmed"]) is bool
        and content["confirmed"] is True
    )


def approval_wire(message):
    """Use the SDK's typed metadata path, not Context.elicit's data-form API."""
    import mcp.types as types

    return types.ElicitRequest(params=types.ElicitRequestFormParams(
        _meta=copy.deepcopy(APPROVAL_META), message=message, requested_schema=copy.deepcopy(SCHEMA),
    ))


class PendingRequests:
    """The synthetic frontend consumes only actually observed, bound requests."""

    def __init__(self):
        self.pending = {}
        self.seen = set()

    @staticmethod
    def key(request_id):
        if type(request_id) not in (int, str):
            raise ValueError("invalid native request id")
        return type(request_id), request_id

    def observe(self, event):
        key = self.key(event.get("id"))
        params = event.get("params")
        if (event.get("method") != "mcpServer/elicitation/request"
                or not isinstance(params, dict) or params.get("mode") != "form"
                or params.get("serverName") != "fixture"
                or not all(isinstance(params.get(k), str) and params[k] for k in ("threadId", "turnId"))
                or not isinstance(params.get("message"), str)
                or not re.match(r"^Fixture approval [0-9a-f]{32}: ", params["message"])):
            raise ValueError("unbound native form")
        if key in self.seen:
            raise ValueError("reused native request id")
        self.seen.add(key)
        self.pending[key] = copy.deepcopy(event)

    def consume(self, event, *, thread, turn):
        key = self.key(event.get("id"))
        original = self.pending.get(key)
        if (original is None or json.dumps(original, sort_keys=True, allow_nan=False)
                != json.dumps(event, sort_keys=True, allow_nan=False)):
            raise ValueError("unknown or changed native request")
        if (event["params"]["threadId"], event["params"]["turnId"]) != (thread, turn):
            raise ValueError("request belongs to another thread or turn")
        return self.pending.pop(key)

    def retire(self, turn=None):
        self.pending = {key: row for key, row in self.pending.items()
                        if turn is not None and row["params"]["turnId"] != turn}


def append_row(root, name, row):
    with (root / name).open("a") as output:
        output.write(json.dumps(row, sort_keys=True) + "\n")


async def perform(ctx, root, case, incarnation, *, unattended=False):
    """One fixed scratch append after a strict response; called only by the fixture."""
    import mcp.types as types
    from mcp.shared.message import ServerMessageMetadata

    from scripts.ci.ego_owned_processes import parse_stat

    nonce = uuid.uuid4().hex

    def record(event, **values):
        append_row(root, "server.jsonl", {
            "event": event, "at": time.monotonic_ns(), "nonce": nonce,
            "incarnation": incarnation, "case": case, **values,
        })

    capabilities = ctx.session.client_capabilities
    record("entered", process=parse_stat(Path("/proc/self/stat").read_text()),
           protocol=str(ctx.session.protocol_version),
           capabilities=capabilities.model_dump(by_alias=True, exclude_none=True) if capabilities else None,
           unattended=unattended)
    if unattended:
        record("role_refused")
        return "fixture-role-refused"
    if str(ctx.session.protocol_version) != "2025-06-18":
        record("unsupported_protocol")
        return "fixture-unsupported-protocol"
    if not ctx.session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())):
        record("unsupported_capability")
        return "fixture-unsupported-capability"
    if type(ctx.request_id) not in (str, int):
        raise ValueError("fixture requires an actual MCP tool request")
    data_mode = case == "data_form_never"
    target = "data-sink.jsonl" if data_mode else "approval-sink.jsonl"
    message = f"Fixture approval {nonce}: append one row to {target} in this isolated fixture."
    if data_mode:
        record("data_requested", message=message)
    else:
        request = approval_wire(message)
        if case == "data_form_empty":
            request = types.ElicitRequest(params=types.ElicitRequestFormParams(
                message=message, requested_schema={"type": "object", "properties": {}},
            ))
        record("requested", wire=request.model_dump(by_alias=True, mode="json", exclude_none=True))
    try:
        if data_mode:
            result = await ctx.elicit(message, bool)
            response = {"action": result.action}
            if result.action == "accept":
                response["content"] = {"value": result.data}
        else:
            result = await ctx.session.send_request(
                request, types.ElicitResult,
                metadata=ServerMessageMetadata(related_request_id=ctx.request_id),
            )
            response = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    except asyncio.CancelledError:
        record("cancelled")
        raise
    except Exception as error:
        record("request_error", error_type=type(error).__name__)
        return "fixture-request-error"
    record("response", response=response)
    if asyncio.current_task().cancelling():
        record("cancelled")
        return "fixture-cancelled"
    data_accepted = data_mode and response == {"action": "accept", "content": {"value": True}}
    if (data_accepted and type(response["content"]["value"]) is bool) or (not data_mode and accepted(response)):
        sink = "data-sink.jsonl" if data_mode else "approval-sink.jsonl"
        append_row(root, sink, {"at": time.monotonic_ns(), "nonce": nonce,
                                "incarnation": incarnation, "case": case})
        record("appended", sink=sink)
        return "fixture-appended"
    record("refused")
    return "fixture-refused"


def serve():
    # This entry is used only inside the qualification container. No production
    # server/config/catalog is changed or imported by this test fixture.
    from fastmcp import Context, FastMCP

    root = Path(os.environ["ELICITATION_ROOT"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("owned fixture root is required")
    case = os.environ["ELICITATION_CASE"]
    incarnation = uuid.uuid4().hex
    mcp = FastMCP("fixture")

    @mcp.tool()
    async def record(ctx: Context) -> str:
        return await perform(ctx, root, case, incarnation,
                             unattended=os.environ.get("GENESIS_CC_SESSION") == "1")

    mcp.run()


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    serve()
