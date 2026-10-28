"""Native CLI context/facade controls using a local synthetic provider.

No account credentials or paid provider requests are used. CI without Claude
Code skips these controls; the build/review session must run them on its pin.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(shutil.which("claude") is None, reason="native Claude CLI unavailable")
@pytest.mark.parametrize("mode", ["json", "stream-json"])
@pytest.mark.parametrize("auth", ["api", "oauth"])
def test_native_context_suppression_preserves_facade(tmp_path, mode, auth, record_property):
    cli = shutil.which("claude")
    version = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=10)
    assert version.returncode == 0
    record_property("native_cli_version", version.stdout.strip())
    home, config, ancestor = (tmp_path / name for name in ("home", "config", "project"))
    for path in (home, config, ancestor):
        path.mkdir()
    cwd = ancestor / "child"
    cwd.mkdir()
    canaries = {name: secrets.token_hex(24) for name in ("user", "ancestor", "project")}
    for path, name in ((config / "CLAUDE.md", "user"), (ancestor / "CLAUDE.md", "ancestor"), (cwd / "CLAUDE.md", "project")):
        path.write_text("Fixture private context: " + canaries[name])
    canaries["attachment"] = secrets.token_hex(24)
    outside = tmp_path / "outside.txt"
    outside.write_text(canaries["attachment"])
    alias = cwd / "alias.txt"
    alias.symlink_to(outside)
    private_directory = tmp_path / "private-directory"
    private_directory.mkdir()
    (private_directory / ("fixture-" + canaries["attachment"] + ".txt")).write_text("fixture")
    mentions = (str(outside), "../../outside.txt", "alias.txt", str(private_directory))
    marker = tmp_path / "facade-called"
    facade = tmp_path / "facade.py"
    facade.write_text(
        "from pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
        "m=FastMCP('genesis_peer')\ndef ping()->str:\n"
        f" Path({str(marker)!r}).write_text('called')\n return 'pong'\nm.tool()(ping)\nm.run()\n"
    )
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"genesis_peer": {"command": sys.executable, "args": ["-P", str(facade)]}}}))
    mcp.chmod(0o600)
    seen = set()
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append(True)
            serialized = json.dumps(body)
            seen.update(name for name, value in canaries.items() if value in serialized)
            if self.path.endswith("/count_tokens"):
                payload = {"input_tokens": 50}
            else:
                done = any(isinstance(block, dict) and block.get("type") == "tool_result" for message in body.get("messages", []) if isinstance(message.get("content"), list) for block in message["content"])
                content = [{"type": "text", "text": "fixture complete"}] if done else [{"type": "tool_use", "id": "fixture_ping", "name": "mcp__genesis_peer__ping", "input": {}}]
                payload = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6", "content": content, "stop_reason": "end_turn" if done else "tool_use", "stop_sequence": None, "usage": {"input_tokens": 50, "output_tokens": 10}}
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config), "PATH": os.defpath,
                   "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
            env["ANTHROPIC_API_KEY" if auth == "api" else "CLAUDE_CODE_OAUTH_TOKEN"] = secrets.token_urlsafe(32)
            args = [cli, "-p", "Perform the fixture ping.", "--model", "claude-sonnet-4-6",
                    "--output-format", mode, "--setting-sources", "user,project,local",
                    "--mcp-config", str(mcp), "--strict-mcp-config", "--tools", "",
                    "--permission-mode", "dontAsk", "--allowedTools", "mcp__genesis_peer__ping",
                    "--disable-slash-commands", "--no-chrome", "--no-session-persistence", "--max-turns", "4"]
            if mode == "stream-json":
                args.append("--verbose")
            # An unsuppressed arm proves that every canary is reachable. A
            # negative-only probe would pass even if the fixture were misplaced.
            for mention in mentions:
                # The production invoker supplies prompts through stdin.
                args[2] = "Perform the fixture ping and consider @" + mention
                for suppress in (False, True):
                    seen.clear()
                    requests.clear()
                    marker.unlink(missing_ok=True)
                    for name in ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_ATTACHMENTS"):
                        if suppress:
                            env[name] = "1"
                        else:
                            env.pop(name, None)
                    prompt = args[2]
                    stdin_args = args[:2] + args[3:]
                    result = subprocess.run(stdin_args, input=prompt, cwd=cwd, env=env, capture_output=True, text=True, timeout=45)
                    assert result.returncode == 0 and marker.exists() and len(requests) >= 2
                    assert (not seen) if suppress else seen == set(canaries)
        finally:
            server.shutdown()
            thread.join(5)
