"""Native Codex fixtures: localhost provider, isolated home, harmless sinks only."""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


class AppServer:
    """Bounded stdio JSON-RPC driver; notifications remain available as evidence."""

    def __init__(self, binary: str, root: Path, env: dict[str, str]):
        self.events: list[dict] = []
        self.pending: queue.Queue = queue.Queue()
        self.stderr = (root / "app-stderr.txt").open("a")
        self.process = subprocess.Popen(  # noqa: S603
            [binary, "app-server", "--listen", "stdio://"], cwd=root / "workspace",
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, start_new_session=True,
        )
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.pending.put(json.loads(line))
            except json.JSONDecodeError:
                self.pending.put({"invalid_json": line})
        self.pending.put({"closed": True})

    def send(self, method: str, params: dict, request_id: int | None = None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            event = self.pending.get(timeout=max(0, deadline - time.monotonic()))
            self.events.append(event)
            assert "closed" not in event and "invalid_json" not in event, event
            assert not ("method" in event and "id" in event), f"Unanswered server request: {event}"
            assert "error" not in event, event
            if predicate(event):
                return event

    def request(self, method: str, params: dict, request_id: int):
        self.send(method, params, request_id)
        return self.until(lambda event: event.get("id") == request_id and "method" not in event)["result"]

    def initialize(self):
        result = self.request("initialize", {
            "clientInfo": {"name": "genesis_native_fixture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }, 1)
        self.send("initialized", {})
        return result

    def close(self):
        # Only our own freshly created process group, never an inherited group.
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


@contextmanager
def fixture_provider(item: dict):
    """Emit exactly one requested native action, then a final response."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            output = item(body, len(requests)) if callable(item) else [item] if len(requests) == 1 else [{
                "type": "message", "id": "msg_fixture", "role": "assistant",
                "content": [{"type": "output_text", "text": "fixture complete", "annotations": []}],
            }]
            events = [
                {"type": "response.created", "response": {"id": "resp_fixture", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": output[0]},
                {"type": "response.output_item.done", "output_index": 0, "item": output[0]},
                {"type": "response.completed", "response": {
                    "id": "resp_fixture", "status": "completed", "output": output,
                }},
            ]
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configure(root: Path, url: str, decision: str) -> dict[str, str]:
    """Create a vetted hook and fake MCP; never inherit credentials or real servers."""
    home = root / "codex"
    home.mkdir()
    (root / "workspace").mkdir()
    hook = root / "hook.py"
    hook.write_text(
        "import json, pathlib, sys\n"
        f"p = pathlib.Path({str(root / 'hooks.jsonl')!r})\n"
        "with p.open('a') as out: out.write(json.dumps(json.load(sys.stdin))+'\\n')\n"
        + ("print('Fixture denies this action', file=sys.stderr)\nsys.exit(2)\n"
           if decision == "deny" else "sys.exit(0)\n")
    )
    mcp = root / "fixture_mcp.py"
    mcp.write_text(
        "from fastmcp import FastMCP\nfrom pathlib import Path\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def record() -> str:\n"
        f"    Path({str(root / 'workspace' / 'mcp-receipt.txt')!r}).write_text('fixture')\n"
        "    return 'fixture'\nmcp.run()\n"
    )
    command = shlex.join([sys.executable, str(hook)])
    (home / "config.toml").write_text(f'''
# TEST FIXTURE ONLY: full access without approval; never reuse for a live model/profile.
model = "gpt-6.1-sol"
model_provider = "fixture"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[analytics]
enabled = false
[features]
plugins = false
remote_plugin = false
[model_providers.fixture]
name = "fixture"
base_url = {json.dumps(url)}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 10000
[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = {json.dumps(command)}
timeout = 5
[mcp_servers.fixture]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(mcp))}]
required = true
startup_timeout_sec = 10
default_tools_approval_mode = "approve"
''')
    return {"PATH": os.environ["PATH"], "HOME": str(root), "CODEX_HOME": str(home)}


def trust_fixture_hook(binary: str, root: Path, env: dict[str, str]):
    """Trust only the exact isolated fixture hash reported by native hooks/list."""
    driver = AppServer(binary, root, env)
    try:
        driver.initialize()
        listing = driver.request("hooks/list", {"cwds": [str(root / "workspace")]}, 2)
        entries = listing["data"]
        assert len(entries) == 1 and not entries[0]["errors"], listing
        hooks = entries[0]["hooks"]
        assert len(hooks) == 1, listing
        hook = hooks[0]
        assert hook["eventName"] == "preToolUse" and hook["trustStatus"] == "untrusted", hook
        with (root / "codex" / "config.toml").open("a") as config:
            config.write(f'\n[hooks.state.{json.dumps(hook["key"])}]\n')
            config.write(f'trusted_hash = {json.dumps(hook["currentHash"])}\n')
    finally:
        driver.close()
    confirmation = AppServer(binary, root, env)
    try:
        confirmation.initialize()
        verified = confirmation.request("hooks/list", {"cwds": [str(root / "workspace")]}, 2)
        assert len(verified["data"]) == 1 and not verified["data"][0]["errors"], verified
        trusted = verified["data"][0]["hooks"]
        assert len(trusted) == 1 and trusted[0]["trustStatus"] == "trusted", verified
        assert trusted[0]["key"] == hook["key"] and trusted[0]["currentHash"] == hook["currentHash"]
        effective = confirmation.request("config/read", {"includeLayers": False}, 3)["config"]
        assert effective["analytics"]["enabled"] is False, effective
        assert effective["model"] == "gpt-6.1-sol", effective
        return {"before": listing, "after": verified, "effective_config": effective}
    finally:
        confirmation.close()


def run_native(binary: str, client: str, root: Path, env: dict[str, str], evidence: dict):
    if client == "cli":
        process = subprocess.Popen(  # noqa: S603
            [binary, "exec", "--ephemeral", "--skip-git-repo-check", "--json",
             "-C", str(root / "workspace"), "Run the isolated fixture."],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        stdout = stderr = ""
        try:
            stdout, stderr = process.communicate(timeout=45)
            assert process.returncode == 0, stderr + stdout
            return [json.loads(line) for line in stdout.splitlines()], stderr
        except subprocess.TimeoutExpired as exc:
            stdout = exc.output or b""
            stderr = exc.stderr or b""
            raise
        finally:
            evidence["stdout"] = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            evidence["stderr"] = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
    driver = AppServer(binary, root, env)
    evidence["events"] = driver.events
    try:
        driver.initialize()
        result = driver.request("thread/start", {
            "cwd": str(root / "workspace"), "ephemeral": True,
        }, 2)
        driver.request("turn/start", {
            "threadId": result["thread"]["id"],
            "input": [{"type": "text", "text": "Run the isolated fixture."}],
        }, 3)
        finished = driver.until(lambda event: event.get("method") == "turn/completed")
        assert finished["params"]["turn"]["status"] == "completed", finished
        return driver.events, (root / "app-stderr.txt").read_text()
    finally:
        driver.close()
        evidence["stderr"] = (root / "app-stderr.txt").read_text()
