#!/usr/bin/env python3
"""Stock mechanics only. Run solely in the disposable, isolated CI image."""

import hashlib
import importlib.metadata
import itertools
import json
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import headlong_fixture as fixture

PIN = "eab04ad4097fb2030f27bee6026e24f57289f12d"
ROUTES = ("fixed", "http", "cli")
KINDS = ("effect", "noop", "failure")
UPSTREAM = [(name, "", 1) for name in (
    "llm_adapter", "monolith_backoff", "monolith_request_model", "dispatcher_cleanup", "recap",
)] + [("dispatcher_single_owner", name, minimum) for name, minimum in (
    ("test_named_start_keeps_kick", 1), ("test_die_after_claim_recovers", 4),
    ("test_crash_after_launch_does_not_double_fire", 4),
)]


def qualify(kind, evidence):
    if not evidence["capture_verified"]:
        return False
    rows, run_id = evidence["trajectory"], evidence["run_id"]
    bound = bool(run_id) and sum(row.get("type") == "shellm-run" and
                                row.get("step_id") == run_id for row in rows) == 1
    finals = [row for row in rows if row.get("type") == "final" and row.get("run_id") == run_id]
    if kind == "failure":
        return bound and evidence["returncode"] != 0 and evidence["effect"] is None and not finals
    return (bound and evidence["returncode"] == 0 and len(finals) == 1
            and finals[0].get("content") == ("done" if kind == "effect" else "no-op")
            and evidence["effect"] == (b"synthetic-effect\n" if kind == "effect" else None))


def upstream_passed(output, minimum):
    if re.search(r"^(?:FAIL|SKIP)\b", output, re.MULTILINE):
        return False
    matches = re.findall(r"(?:cases: pass=(\d+) fail=(\d+) skip=(\d+)|(\d+) passed, (\d+) failed)", output)
    if len(matches) != 1:
        return False
    p, f, s, p2, f2 = matches[0]
    return int(p or p2) >= minimum and int(f or f2) == 0 and int(s or "0") == 0


def tree_hash(root):
    return hashlib.sha256(json.dumps([
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*")) if path.is_file()
    ]).encode()).hexdigest()


def dependency_inventory(root=Path("/fixture")):
    return {"os": (root / "packages.txt").read_text(),
            "python": (root / "python-packages.txt").read_text()}


def preflight():
    if os.getuid() != 65532 or Path("/var/run/docker.sock").exists():
        raise ValueError("required unprivileged/no-socket boundary missing")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if int(status["CapEff"].strip(), 16) or status["NoNewPrivs"].strip() != "1":
        raise ValueError("capability or privilege boundary missing")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    if not any(row[4] == "/" and "ro" in row[5].split(",") for row in mounts):
        raise ValueError("root filesystem is not readonly")
    if sorted(path.name for path in Path("/sys/class/net").iterdir()) != ["lo"]:
        raise ValueError("network is not loopback-only")
    if any(re.search(r"(?:API_KEY|TOKEN|SECRET|PASSWORD)$", name) and value
           for name, value in os.environ.items()):
        raise ValueError("credential-like environment present")
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1):
            raise ValueError("external network unexpectedly reachable")
    except OSError:
        pass
    if Path("/headlong/.source-pin").read_text().strip() != PIN:
        raise ValueError("unexpected upstream source pin")


def environment(root):
    (root / "home").mkdir()
    return {"PATH": "/headlong/bin:/headlong/tools:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": str(root / "home"), "HEADLONG_HOME": str(root / "state"), "TMPDIR": str(root),
            "SHELLM_ENV": "local", "LLM_RETRIES": "0", "LLM_MAX_TIME": "10",
            "SHELLM_MAX_EXEC_TIME": "10", "PYTHONDONTWRITEBYTECODE": "1"}


def run_upstream(root):
    results = []
    for index, (name, selected, minimum) in enumerate(UPSTREAM):
        child = root / f"upstream-{index}"
        child.mkdir()
        env = environment(child)
        if selected:
            env["TEST_FILTER"] = selected
        result = subprocess.run(["bash", f"/headlong/tests/test_{name}.sh"], env=env,
                                cwd=child, capture_output=True, text=True, timeout=180)
        results.append({"test": name, "filter": selected, "returncode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr,
                        "passed": result.returncode == 0 and upstream_passed(result.stdout, minimum)})
    return results


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def request_verified(route, captures, marker):
    main = [row for row in captures if not row["system"].startswith("You produce concise summaries as JSON.")
            and marker in json.dumps(row["messages"])]
    required = {"http"} if route == "http" else {"adapter", "fake-cli"} if route == "cli" else {"adapter"}
    if {row["source"] for row in main} != required or any(not row["system"] for row in main):
        return False
    if route == "cli":
        original = [row for row in main if row["source"] == "adapter"]
        forwarded = [row for row in main if row["source"] == "fake-cli"]
        return (len(original) == len(forwarded) == 1 and original[0]["messages"] == forwarded[0]["messages"]
                and original[0]["system"] == forwarded[0]["system"])
    return len(main) == 1


def run_route(root, route, kind):
    root.mkdir()
    env = environment(root)
    capture = root / "capture.jsonl"
    env.update(CONTROL_KIND=kind, CONTROL_CAPTURE=str(capture), TEXT_ROUTE=route,
               SHELLM_RUN_ID_OUT=str(root / "run-id"), SHELLM_MODEL="synthetic")
    server = thread = None
    http_captures = []
    if route == "http":
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                system = "\n".join(row["content"] for row in payload["messages"] if row["role"] == "system")
                messages = [row for row in payload["messages"] if row["role"] != "system"]
                http_captures.append({"source": "http", "system": system, "messages": messages})
                try:
                    text = (fixture.response(kind) if not system.startswith("You produce concise summaries as JSON.")
                            else '{"tldr":"synthetic summary","full_summary":null}')
                except ValueError:
                    self.send_error(503, "synthetic provider failure")
                    return
                data = ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) +
                        "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        env.update(LLM_PROVIDER="openai-compatible",
                   SHELLM_API_URL=f"http://127.0.0.1:{server.server_port}/v1/chat/completions")
    else:
        env.update(LLM_PROVIDER="adapter", LLM_ADAPTER="/fixture/headlong_fixture.py")
    marker = f"synthetic-{route}-{kind}"
    try:
        result = subprocess.run(["/headlong/bin/shellm", "--env", "local", "--max-iterations", "1",
                                 "--workdir", str(root), "--traj-dir", str(root / "trajectories"),
                                 "-q", marker], env=env, cwd=root, capture_output=True, text=True, timeout=30)
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join()
    captures = http_captures if route == "http" else read_jsonl(capture)
    rows = [row for path in (root / "trajectories").rglob("trajectory.jsonl") for row in read_jsonl(path)]
    evidence = {"returncode": result.returncode, "run_id": (root / "run-id").read_text().strip(),
                "effect": (root / "effect.txt").read_bytes() if (root / "effect.txt").exists() else None,
                "trajectory": rows, "capture_verified": request_verified(route, captures, marker)}
    passed = qualify(kind, evidence)
    evidence["effect"] = evidence["effect"].decode() if evidence["effect"] is not None else None
    return {"route": route, "control": kind, "passed": passed, **evidence, "captures": captures,
            "stdout": result.stdout, "stderr": result.stderr}


def main():
    preflight()  # Must pass before invoking any upstream program.
    before = {"headlong": tree_hash(Path("/headlong")), "fixtures": tree_hash(Path("/fixture"))}
    with TemporaryDirectory(dir="/work") as directory:
        root = Path(directory)
        upstream = run_upstream(root)
        routes = [run_route(root / f"{route}-{kind}", route, kind)
                  for route, kind in itertools.product(ROUTES, KINDS)]
    if before != {"headlong": tree_hash(Path("/headlong")), "fixtures": tree_hash(Path("/fixture"))}:
        raise ValueError("sources changed during execution")
    passed = all(row["passed"] for row in upstream + routes)
    print(json.dumps({"schema": "headlong-stock-controls/v1", "status": "pass" if passed else "fail",
                      "source_pin": PIN, "sources": before, "sdk": importlib.metadata.version("claude-agent-sdk"),
                      "dependencies": dependency_inventory(),
                      "upstream": upstream, "routes": routes, "route_total": len(routes),
                      "route_passed": sum(row["passed"] for row in routes), "cleanup_confirmed": True,
                      "limits": "Stock mechanics and fake text routes only; no real CLI, model, authority or ego parity."}))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "reason": str(exc)}))
        raise SystemExit(2) from exc
