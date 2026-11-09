"""Exercise framing without starting any native binary or Genesis service."""

import io
import json
import queue
from types import SimpleNamespace

import pytest

from tests.test_hooks.native_codex import AppServer


def driver_with(*events):
    driver = AppServer.__new__(AppServer)
    driver.events = []
    driver.pending = queue.Queue()
    driver.process = SimpleNamespace(stdin=io.StringIO())
    for event in events:
        driver.pending.put(event)
    return driver


def test_unexpected_request_still_refuses():
    driver = driver_with({"method": "unexpected", "id": 4})
    with pytest.raises(AssertionError, match="Unanswered server request"):
        driver.until(lambda event: True)


@pytest.mark.parametrize("request_id", [7, "request-7"])
def test_explicit_request_reply_preserves_id_and_notifications(request_id):
    request = {"method": "item/commandExecution/requestApproval", "id": request_id, "params": {}}
    notification = {"method": "turn/started", "params": {}}
    driver = driver_with(notification, request)
    observed = driver.until(lambda event: "id" in event, allow_requests=True)
    driver.reply(observed, {"decision": "decline"})
    assert driver.events == [notification, request]
    assert json.loads(driver.process.stdin.getvalue()) == {"id": request_id, "result": {"decision": "decline"}}


@pytest.mark.parametrize("frame", [{"method": "notification"}, {"id": 3}, {"method": "bad", "id": True}])
def test_reply_refuses_non_request(frame):
    driver = driver_with()
    with pytest.raises(AssertionError):
        driver.reply(frame, {})
    assert driver.process.stdin.getvalue() == ""


def test_expected_rpc_error_is_explicit():
    event = {"id": 5, "error": {"code": -32000, "message": "fixture failure"}}
    with pytest.raises(AssertionError):
        driver_with(event).until(lambda item: item.get("id") == 5)
    assert driver_with(event).until(lambda item: item.get("id") == 5, allow_error=True) == event


@pytest.mark.parametrize("event", [{"closed": True}, {"invalid_json": "bad"}])
def test_failed_transport_never_satisfies_predicate(event):
    with pytest.raises(AssertionError):
        driver_with(event).until(lambda item: True, allow_requests=True, allow_error=True)


@pytest.mark.parametrize("early", [False, True])
def test_explicit_approval_survives_start_response_order(early):
    approval = {"id": "approval", "method": "item/commandExecution/requestApproval", "params": {}}
    response = {"id": 3, "result": {"turn": {"id": "turn"}}}
    driver = driver_with(*( (approval, response) if early else (response, approval)))
    assert driver.request("turn/start", {}, 3, allow_requests=True) == response["result"]
    observed = next((e for e in driver.events if "method" in e and "id" in e), None)
    observed = observed or driver.until(lambda e: "method" in e and "id" in e, allow_requests=True)
    driver.reply(observed, {"decision": "decline"})
    assert json.loads(driver.process.stdin.getvalue().splitlines()[-1])["id"] == "approval"


@pytest.mark.parametrize("mode", ["default", "empty_item", "scripted", "no_actions"])
def test_fixture_provider_scripted_actions_preserve_default_and_empty_items(mode):
    import urllib.request

    from tests.test_hooks.native_codex import fixture_provider

    first, second = {"type": "function_call", "id": "first"}, {"type": "function_call", "id": "second"}
    item = {} if mode == "empty_item" else first
    options = {"action_plan": {1: first, 3: second} if mode == "scripted" else {}}
    if mode in {"default", "empty_item"}:
        options = {}
    with fixture_provider(item, **options) as (url, requests):
        outputs = []
        for n in range(4):
            request = urllib.request.Request(  # noqa: S310 - URL from the owned localhost fixture.
                url + "/responses", data=json.dumps({"probe": n}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as reply:  # noqa: S310 - Same fixed localhost URL.
                events = [json.loads(line[6:]) for line in reply.read().decode().splitlines()
                          if line.startswith("data: ")]
            outputs.append(events[-1]["response"]["output"][0])
        assert len(requests) == 4
    if mode == "scripted":
        assert [outputs[i] for i in (0, 2)] == [first, second]
        assert [outputs[i]["type"] for i in (1, 3)] == ["message", "message"]
    elif mode == "no_actions":
        assert [row["type"] for row in outputs] == ["message"] * 4
    else:
        assert outputs[0] == item
        assert [row["type"] for row in outputs[1:]] == ["message"] * 3


@pytest.mark.parametrize("replay_at", ["never", "resume_only", "first_close", "resume", "close", "close_request"])
def test_resume_measurement_detects_repeated_action(tmp_path, monkeypatch, replay_at):
    """Exercise the real measurement with a cached replay and no provider request."""
    import shlex
    from contextlib import nullcontext
    from unittest.mock import Mock

    from tests.test_hooks import test_codex_compatibility_native as native

    sink = tmp_path / "workspace/resume.txt"
    sink.parent.mkdir()
    requests = [{}, {}]
    provider = Mock(return_value=nullcontext(("fixture", requests)))

    def execute():
        item = provider.call_args.args[0]
        command = shlex.split(json.loads(item["arguments"])["cmd"])
        assert command[:2] == ["printf", "fixture"] and command[-1] == "resume.txt"
        with sink.open({">": "w", ">>": "a"}[command[-2]]) as output:
            output.write("fixture")

    def begin(*args, **kwargs):
        if replay_at != "resume_only":
            execute()
        return "thread", "turn"

    def resume(*args):
        if replay_at in {"resume", "resume_only"}:
            execute()
        return {"thread": {"id": "thread"}}

    def close_resumed():
        if replay_at == "close":
            execute()
        if replay_at == "close_request":
            requests.append({})

    first = SimpleNamespace(events=[], close=lambda: execute() if replay_at == "first_close" else None)
    resumed = SimpleNamespace(events=[], initialize=Mock(), request=resume,
                              close=close_resumed)
    monkeypatch.setattr(native, "fixture_provider", provider)
    monkeypatch.setattr(native, "configure", lambda *args: {})
    monkeypatch.setattr(native, "trust_fixture_hook", Mock())
    monkeypatch.setattr(native, "AppServer", Mock(side_effect=[first, resumed]))
    monkeypatch.setattr(native, "begin", begin)
    monkeypatch.setattr(native, "completed", lambda *args: {"params": {"turn": {"status": "completed"}}})
    expected = nullcontext() if replay_at == "never" else pytest.raises(AssertionError)
    with expected:
        native.test_native_resume(tmp_path, {"app": "unused"}, {})
