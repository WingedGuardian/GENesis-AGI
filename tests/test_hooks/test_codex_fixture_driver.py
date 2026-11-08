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
