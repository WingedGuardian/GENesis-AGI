"""Synthetic frontend around the inherited native driver, with observable replies."""

import copy
import queue
import time

from tests.test_hooks.codex_elicitation_fixture import PendingRequests


class ElicitationDriver:
    def __init__(self, transport, root):
        self.transport = transport
        self.root = root
        self.events = transport.events
        self.sent = []
        self.observed = []
        self.observations = []
        self.consents = PendingRequests()

    def initialize(self):
        result = self.request("initialize", {
            "clientInfo": {"name": "genesis_native_fixture", "version": "1"},
            "capabilities": {"experimentalApi": True,
                             "extensions": {"openai/standard-form-input": {}}},
        }, 1)
        self.send("initialized", {})
        return result

    def start_thread(self, cwd):
        # Codex 0.166 thread_manager.rs:2641-2645 requires both the client
        # extension and a root user thread for data entry under Never.
        return self.request("thread/start", {
            "cwd": str(cwd), "ephemeral": True, "threadSource": "user",
        }, 2)

    def send(self, method, params, request_id=None):
        packet = {"method": method, "params": copy.deepcopy(params)}
        if request_id is not None:
            packet["id"] = request_id
        self.sent.append({"at": time.monotonic_ns(), "message": packet})
        self.transport.send(method, params, request_id)

    def until(self, predicate, **kwargs):
        def observed(event):
            if event.get("method") == "mcpServer/elicitation/request":
                self.consents.observe(event)
                self.observed.append({"at": time.monotonic_ns(), "message": copy.deepcopy(event)})
            elif "method" in event and "id" in event:
                raise ValueError("unexpected native approval route")
            return predicate(event)

        return self.transport.until(observed, **kwargs)

    def request(self, method, params, request_id, *, allow_requests=False):
        self.send(method, params, request_id)
        return self.until(
            lambda event: event.get("id") == request_id and "method" not in event,
            allow_requests=allow_requests,
        )["result"]

    def snapshot(self, label):
        row = {"at": time.monotonic_ns(), "label": label}
        for name in ("approval", "data"):
            path = self.root / f"{name}-sink.jsonl"
            row[name] = path.read_bytes().hex() if path.exists() else None
        self.observations.append(row)
        return row

    def reply(self, request, result, *, thread, turn):
        original = self.consents.consume(request, thread=thread, turn=turn)
        self.snapshot("before-response")
        self.sent.append({"at": time.monotonic_ns(), "message": {
            "id": original["id"], "result": copy.deepcopy(result),
        }, "synthetic_frontend": True})
        self.transport.reply(original, result)

    def raw_reply(self, request_id, result):
        # Explicit negative injection: never present this as a normal native ask.
        self.snapshot("before-negative-injection")
        self.sent.append({"at": time.monotonic_ns(), "message": {
            "id": request_id, "result": copy.deepcopy(result),
        }, "negative_injection": True})
        self.transport.reply({"id": request_id, "method": "mcpServer/elicitation/request"}, result)

    def close(self):
        self.snapshot("before-close")
        self.consents.retire()
        self.transport.close()
        # The inherited reader queues late stdout through shutdown. Retain it
        # before observing final effects; no pending post-close witness is lost.
        while True:
            try:
                self.events.append(self.transport.pending.get_nowait())
            except queue.Empty:
                break
        self.snapshot("after-close")
