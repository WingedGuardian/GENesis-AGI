"""Fixture consent must not be inferred from truthiness or reusable approval."""

import asyncio
import copy
import json
import os
import queue
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_hooks.codex_elicitation_driver import ElicitationDriver
from tests.test_hooks.codex_elicitation_fixture import (
    PendingRequests,
    accepted,
    approval_wire,
    perform,
)


@pytest.mark.parametrize("response,wanted", [
    ({"action": "accept", "content": {"confirmed": True}}, True),
    ({"action": "accept", "content": {"confirmed": False}}, False),
    ({"action": "accept", "content": {"confirmed": 1}}, False),
    ({"action": "accept", "content": {"confirmed": "true"}}, False),
    ({"action": "accept", "content": {}}, False),
    ({"action": "accept", "content": None}, False),
    ({"action": "accept", "content": {"confirmed": True, "approved": True}}, False),
    ({"action": "decline", "content": {"confirmed": True}}, False),
    ({"action": "cancel", "content": {"confirmed": True}}, False),
    ({"action": "accept", "content": {"confirmed": True}, "_meta": {"persist": "session"}}, False),
    ({"action": "accept", "content": {"confirmed": True}, "_meta": {"persist": "always"}}, False),
    ({"action": "accept", "content": {"confirmed": True}, "_meta": {"approvals_reviewer": "auto_review"}}, False),
    (None, False),
    ([], False),
])
def test_only_exact_fresh_boolean_acceptance_counts(response, wanted):
    assert accepted(response) is wanted


def request(request_id=1, turn="turn-1"):
    return {"id": request_id, "method": "mcpServer/elicitation/request", "params": {
        "threadId": "thread-1", "turnId": turn, "serverName": "fixture", "mode": "form",
        "message": "Fixture approval " + "a" * 32 + ": append one harmless row",
    }}


def test_native_request_id_is_consumed_once_and_not_reused():
    pending = PendingRequests()
    event = request()
    pending.observe(event)
    assert pending.consume(event, thread="thread-1", turn="turn-1") == event
    with pytest.raises(ValueError, match="unknown"):
        pending.consume(event, thread="thread-1", turn="turn-1")
    with pytest.raises(ValueError, match="reused"):
        pending.observe(event)


@pytest.mark.parametrize("kind", ["id", "thread", "turn", "body", "bool_id", "null_turn"])
def test_foreign_or_changed_request_cannot_consume_the_observed_one(kind):
    pending = PendingRequests()
    original = request()
    pending.observe(original)
    other = copy.deepcopy(original)
    if kind == "bool_id":
        other["id"] = True
    elif kind == "id":
        other["id"] = "1"  # String and integer are distinct native request IDs.
    elif kind == "thread":
        other["params"]["threadId"] = "thread-2"
    elif kind == "turn":
        other["params"]["turnId"] = "turn-2"
    elif kind == "null_turn":
        other["params"]["turnId"] = None
    else:
        other["params"]["message"] += " changed action"
    with pytest.raises(ValueError):
        pending.consume(other, thread="thread-1", turn="turn-1")
    assert pending.consume(original, thread="thread-1", turn="turn-1") == original


def test_interrupt_retires_only_its_turn_and_close_retires_all():
    pending = PendingRequests()
    first, second = request(), request(2, "turn-2")
    pending.observe(first)
    pending.observe(second)
    pending.retire("turn-1")
    with pytest.raises(ValueError):
        pending.consume(first, thread="thread-1", turn="turn-1")
    assert pending.consume(second, thread="thread-1", turn="turn-2") == second
    third = request(3, "turn-3")
    pending.observe(third)
    pending.retire()
    with pytest.raises(ValueError):
        pending.consume(third, thread="thread-1", turn="turn-3")


def test_changed_boolean_metadata_cannot_consume_original_request():
    pending = PendingRequests()
    original = request()
    original["params"]["_meta"] = {"codex_requires_user_input": True}
    pending.observe(original)
    changed = copy.deepcopy(original)
    changed["params"]["_meta"]["codex_requires_user_input"] = 1
    with pytest.raises(ValueError):
        pending.consume(changed, thread="thread-1", turn="turn-1")
    assert pending.consume(original, thread="thread-1", turn="turn-1") == original


def test_typed_request_keeps_approval_metadata_and_requires_nondefault_boolean():
    wire = approval_wire("Fixture approval " + "a" * 32 + ": append one harmless row").model_dump(
        by_alias=True, mode="json", exclude_none=True,
    )
    assert wire["method"] == "elicitation/create"
    assert wire["params"]["mode"] == "form"
    assert wire["params"]["_meta"]["codex_approval_kind"] == "mcp_tool_call"
    assert wire["params"]["_meta"]["codex_requires_user_input"] is True
    schema = wire["params"]["requestedSchema"]
    assert schema["required"] == ["confirmed"]
    assert schema["properties"]["confirmed"]["type"] == "boolean"
    assert "default" not in schema["properties"]["confirmed"]


class FakeSession:
    def __init__(self, response, *, protocol="2025-06-18", capable=True):
        import mcp.types as types

        self.response = response
        self.protocol_version = protocol
        self.client_capabilities = types.ClientCapabilities(elicitation=types.ElicitationCapability())
        self.capable = capable
        self.calls = []

    def check_client_capability(self, capability):
        assert capability.elicitation is not None
        return self.capable

    async def send_request(self, request, response_type, *, metadata):
        from mcp.server.session import ServerSession

        assert metadata.related_request_id == 7
        owner = self

        class Channel:
            async def send_raw_request(self, method, params, options):
                owner.calls.append({"method": method, "params": params})
                if isinstance(owner.response, BaseException):
                    raise owner.response
                return copy.deepcopy(owner.response)

        class Standalone:
            async def send_raw_request(self, *args):
                raise AssertionError("elicitation escaped the request-scoped channel")

        connection = SimpleNamespace(protocol_version=self.protocol_version, outbound=Standalone())
        # Use the real SDK serializer and per-version result validator; only
        # its transport is fake. No server, native agent or provider is started.
        return await ServerSession(Channel(), connection).send_request(request, response_type, metadata=metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,allowed", [
    ({"action": "accept", "content": {"confirmed": True}}, True),
    ({"action": "accept", "content": {"confirmed": False}}, False),
    ({"action": "accept", "content": {"confirmed": 1}}, False),
    ({"action": "accept", "content": {"confirmed": "true"}}, False),
    ({"action": "accept", "content": {}}, False),
    ({"action": "cancel"}, False),
    ({"action": "decline"}, False),
    ({"action": "accept", "content": {"confirmed": True}, "_meta": {"persist": "session"}}, False),
])
async def test_server_effect_requires_bound_strict_response(tmp_path, response, allowed):
    session = FakeSession(response)
    ctx = SimpleNamespace(session=session, request_id=7)
    outcome = await perform(ctx, tmp_path, "approval_accept_true", "b" * 32)
    rows = [json.loads(line) for line in (tmp_path / "server.jsonl").read_text().splitlines()]
    assert len(session.calls) == 1
    assert (tmp_path / "approval-sink.jsonl").exists() is allowed
    assert outcome == ("fixture-appended" if allowed else "fixture-refused")
    if allowed:
        sink = [json.loads(line) for line in (tmp_path / "approval-sink.jsonl").read_text().splitlines()]
        assert len(sink) == 1
        response_at = next(row["at"] for row in rows if row["event"] == "response")
        assert response_at <= sink[0]["at"] < rows[-1]["at"]
        assert sink[0]["nonce"] == rows[0]["nonce"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unattended", "modern", "no_capability"])
async def test_fixture_refuses_before_requesting_unsupported_or_unattended_input(tmp_path, boundary):
    session = FakeSession({"action": "accept", "content": {"confirmed": True}},
                          protocol="2026-07-28" if boundary == "modern" else "2025-06-18",
                          capable=boundary != "no_capability")
    ctx = SimpleNamespace(session=session, request_id=7)
    outcome = await perform(ctx, tmp_path, "approval_accept_true", "b" * 32,
                            unattended=boundary == "unattended")
    assert outcome in {"fixture-role-refused", "fixture-unsupported-protocol", "fixture-unsupported-capability"}
    assert session.calls == []
    assert not (tmp_path / "approval-sink.jsonl").exists()


@pytest.mark.asyncio
async def test_cancellation_while_waiting_never_appends(tmp_path):
    session = FakeSession(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await perform(SimpleNamespace(session=session, request_id=7), tmp_path,
                      "approval_interrupt", "b" * 32)
    assert not (tmp_path / "approval-sink.jsonl").exists()


def test_frontend_writes_one_reply_and_keeps_shutdown_observations(tmp_path):
    class Transport:
        def __init__(self):
            self.events = []
            self.pending = queue.Queue()
            self.replies = []

        def until(self, predicate, **kwargs):
            event = request()
            self.events.append(event)
            assert predicate(event)
            return event

        def reply(self, event, result):
            self.replies.append((event, result))

        def close(self):
            (tmp_path / "approval-sink.jsonl").write_text("late-shutdown-witness\n")
            self.pending.put({"closed": True})

    transport = Transport()
    driver = ElicitationDriver(transport, tmp_path)
    event = driver.until(lambda row: "method" in row)
    result = {"action": "accept", "content": {"confirmed": True}}
    driver.reply(event, result, thread="thread-1", turn="turn-1")
    with pytest.raises(ValueError):
        driver.reply(event, result, thread="thread-1", turn="turn-1")
    assert len(transport.replies) == 1
    assert driver.observations[0]["approval"] is None
    driver.close()
    assert driver.events[-1] == {"closed": True}
    assert driver.observations[-1]["approval"] == b"late-shutdown-witness\n".hex()


def test_actual_fixture_entry_runs_from_foreign_cwd_without_native_or_listener(tmp_path):
    helper = Path(__file__).with_name("codex_elicitation_fixture.py")
    program = textwrap.dedent('''
        import asyncio, runpy, sys, types
        import mcp.types as wire
        class Context:
            request_id = 7
        class Session:
            protocol_version = "2025-06-18"
            client_capabilities = wire.ClientCapabilities(elicitation=wire.ElicitationCapability())
            def check_client_capability(self, capability):
                return True
            async def send_request(self, request, response_type, **kwargs):
                assert kwargs["metadata"].related_request_id == 7
                return response_type(action="accept", content={"confirmed": True})
        Context.session = Session()
        class FastMCP:
            def __init__(self, name):
                assert name == "fixture"
            def tool(self):
                def register(fn):
                    assert fn.__annotations__["ctx"] is Context
                    self.fn = fn
                    return fn
                return register
            def run(self):
                asyncio.run(self.fn(Context()))
        fake = types.ModuleType("fastmcp")
        fake.Context, fake.FastMCP = Context, FastMCP
        sys.modules["fastmcp"] = fake
        runpy.run_path(sys.argv[1], run_name="__main__")
    ''')
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "ELICITATION_ROOT": str(tmp_path), "ELICITATION_CASE": "approval_accept_true"}
    result = subprocess.run([sys.executable, "-c", program, str(helper)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    rows = (tmp_path / "approval-sink.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["case"] == "approval_accept_true"
