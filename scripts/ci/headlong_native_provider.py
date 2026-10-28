"""Loopback-only, scripted Anthropic Messages backend for contained native probes."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL = "mcp__fixture__claim_effect"


def stream_response(model, blocks):
    events = [{"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant",
               "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 1}}}]
    for index, block in enumerate(blocks):
        is_tool = block["type"] == "tool_use"
        start = {**block, "input": {}} if is_tool else {"type": "text", "text": ""}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if is_tool else {
            "type": "text_delta", "text": block["text"]}
        events.extend([{"type": "content_block_start", "index": index, "content_block": start},
                       {"type": "content_block_delta", "index": index, "delta": delta},
                       {"type": "content_block_stop", "index": index}])
    events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use" if any(
        block["type"] == "tool_use" for block in blocks) else "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1}}, {"type": "message_stop"}])
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


class Backend:
    def __init__(self, kind, port):
        self.kind, self.port = kind, port
        self.evidence = {"main_requests": 0, "provider_error": False, "unexpected": [], "tool_results": [], "captures": []}
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    code, data = backend.respond(self.path, payload)
                except Exception as exc:
                    backend.evidence["unexpected"].append(f"{type(exc).__name__}: {exc}")
                    code, data = 400, json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                                                 "message": "fixture protocol mismatch"}}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream" if code == 200 else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def respond(self, path, payload):
        self.evidence["captures"].append({"path": path, "body": payload})
        if path.split("?")[0] != "/v1/messages":
            raise ValueError(f"unexpected API path: {path}")
        messages = payload["messages"]
        if f"fixture-native-{self.kind}" not in json.dumps(messages):
            raise ValueError("unclassified API request")
        self.evidence["main_requests"] += 1
        if self.kind == "provider_error":
            self.evidence["provider_error"] = True
            return 400, json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                               "message": "synthetic provider failure"}}).encode()
        count = 0 if self.kind == "echo" else 2 if self.kind == "duplicate" else 1
        expected = [f"toolu_fixture_{i + 1}" for i in range(count)]
        results = [block["tool_use_id"] for row in messages if row["role"] == "user"
                   and isinstance(row["content"], list) for block in row["content"] if block["type"] == "tool_result"]
        if not results and count:
            if self.evidence["main_requests"] != 1:
                raise ValueError("native did not deliver requested tool results")
            registered = [tool for tool in payload.get("tools", []) if tool["name"] == TOOL]
            if len(registered) != 1 or registered[0]["input_schema"].get("required") != ["proposal_id"]:
                raise ValueError("protected MCP tool/schema missing")
            blocks = [{"type": "tool_use", "id": identifier, "name": TOOL,
                       "input": {"proposal_id": "missing" if self.kind == "unknown" else "proposal"}} for identifier in expected]
        else:
            if sorted(results) != expected or len(self.port.calls) != count:
                raise ValueError("tool results/actual handler dispositions missing")
            self.evidence["tool_results"] = results
            blocks = [{"type": "text", "text": f"native-final-{self.kind}"}]
        return 200, stream_response(payload["model"], blocks)
