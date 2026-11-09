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


def test_reader_records_completion_before_queueing_without_aliasing(monkeypatch):
    from tests.test_hooks import native_codex as native

    terminal = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn"}}}
    driver = driver_with()
    driver.completions = []
    driver.process.stdout = io.StringIO(json.dumps(terminal) + "\n")
    original_put = driver.pending.put

    def put(message):
        if message.get("method") == "turn/completed":
            assert driver.completions == [{"at": 123, "message": terminal}]
        original_put(message)

    monkeypatch.setattr(native.time, "monotonic_ns", lambda: 123)
    monkeypatch.setattr(driver.pending, "put", put)
    driver._read()
    assert driver.events == []
    message = driver.until(lambda row: row.get("method") == "turn/completed")
    message["params"]["turn"]["id"] = "changed"
    assert driver.completions[0]["message"] == terminal


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


def test_concurrent_provider_actions_bind_to_their_own_append_index(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock, get_ident
    from urllib.request import Request, urlopen

    from tests.test_hooks import native_codex as native

    first_appended, second_contended = Event(), Event()
    locks = []

    class ControlledRequests(list):
        def append(self, body):
            assert locks[0].owner == get_ident(), "append outside request lock"
            super().append(body)
            if body["probe"] == 1:
                first_appended.set()
                assert second_contended.wait(10)
            elif body["probe"] == 2:
                second_contended.set()

        def __len__(self):
            assert locks[0].owner == get_ident(), "index read outside request lock"
            return super().__len__()

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()
            self.owner = None
            locks.append(self)

        def __enter__(self):
            if first_appended.is_set():
                second_contended.set()
            self.lock.acquire()
            self.owner = get_ident()

        def __exit__(self, *args):
            self.owner = None
            self.lock.release()

    class CapturedServer(native.ThreadingHTTPServer):
        def __init__(self, address, handler):
            cells = dict(zip(handler.do_POST.__code__.co_freevars, handler.do_POST.__closure__, strict=True))
            assert cells["requests"].cell_contents == []
            cells["requests"].cell_contents = ControlledRequests()
            super().__init__(address, handler)

    def post(url, probe):
        request = Request(url + "/responses", data=json.dumps({"probe": probe}).encode(),  # noqa: S310
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:  # noqa: S310 - owned loopback server only.
            events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
        return events[-1]["response"]["output"][0]

    monkeypatch.setattr(native, "ThreadingHTTPServer", CapturedServer)
    monkeypatch.setattr(native, "Lock", ObservedLock, raising=False)
    actions = {index: {"type": "function_call", "id": f"action-{index}"} for index in (1, 2, 3)}
    with native.fixture_provider(actions[1], action_plan=actions) as (url, requests):
        assert post(url, 0) == actions[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post, url, 1)
            assert first_appended.wait(10)
            second = pool.submit(post, url, 2)
            actual = [first.result(), second.result()]
        assert requests == [{"probe": index} for index in (0, 1, 2)]
        assert actual == [actions[2], actions[3]]


@pytest.mark.parametrize("damage", [None, "root_dead", "writer_dead", "reused_writer", "detached", "already_terminal"])
def test_eof_input_capture_rechecks_live_pending_owned_writer(tmp_path, monkeypatch, damage):
    import copy

    from tests.test_hooks import test_codex_elicitation_native as native
    from tests.test_hooks.codex_elicitation_driver import ElicitationDriver
    from tests.test_hooks.test_codex_elicitation_readback import golden, persist

    evidence, sinks = golden(tmp_path, "approval_eof")
    population = {10: copy.deepcopy(evidence["driver_before"]), 20: copy.deepcopy(evidence["owned"][0]["before"])}
    ask = evidence["observed"][0]["message"]
    driver = ElicitationDriver(SimpleNamespace(events=[], process=SimpleNamespace(pid=10)), tmp_path)
    driver.consents.observe(ask)
    monkeypatch.setattr(native, "census", lambda: population)
    if damage == "root_dead":
        population[10]["state"] = "Z"
    elif damage == "writer_dead":
        population[20]["state"] = "Z"
    elif damage == "reused_writer":
        population[20]["start"] += 1
    elif damage == "detached":
        population[20]["ppid"] = 99
    elif damage == "already_terminal":
        evidence["server"].append({**evidence["server"][-1], "event": "cancelled"})
        persist(tmp_path, evidence, sinks)
    if damage:
        with pytest.raises(AssertionError):
            native.capture_eof_live(driver, tmp_path, evidence, "thread", "turn-0")
        assert not driver.observations
    else:
        native.capture_eof_live(driver, tmp_path, evidence, "thread", "turn-0")
        assert evidence["eof_live"]["writers"] == [population[20]]
        assert evidence["eof_live"]["ancestries"] == [[population[20], population[10]]]
        assert evidence["eof_live"]["at"] < driver.observations[0]["at"] < evidence["eof_input_at"]
        assert driver.observations[0]["label"] == "before-native-eof"


@pytest.mark.parametrize("state", ["natural", "after_close", "survivor"])
def test_natural_eof_capture_precedes_cleanup_and_freezes_children(tmp_path, monkeypatch, state):
    from tests.test_hooks import test_codex_elicitation_native as native
    from tests.test_hooks.codex_elicitation_driver import ElicitationDriver

    driver = ElicitationDriver(SimpleNamespace(events=[]), tmp_path)
    evidence = {"owned": [{"before": {"pid": 20, "start": 20}, "ancestry": [20, 10]}]}
    called = []

    def retirement(value):
        called.append(True)
        if state == "survivor":
            raise RuntimeError("owned child survived")
        value["owned"][0]["after"] = {"state": "absent"}

    monkeypatch.setattr(native, "observe_retirement", retirement)
    if state == "after_close":
        driver.snapshot("before-close")
        with pytest.raises(AssertionError):
            native.capture_natural_eof(driver, evidence)
        assert not called
    elif state == "survivor":
        with pytest.raises(RuntimeError, match="survived"):
            native.capture_natural_eof(driver, evidence)
        assert "eof_natural" not in evidence and not driver.observations
    else:
        native.capture_natural_eof(driver, evidence)
        evidence["owned"][0]["after"]["state"] = "S"
        assert evidence["eof_natural"][0]["after"] == {"state": "absent"}
        assert [row["label"] for row in driver.observations] == ["native-eof-retired"]
