"""Standalone scratch qualification; never invoke inside a pytest-held lock.

Run with the Genesis venv under hostmetrics, passing a NEW private --root.
Uses actual Codex, a localhost fake provider, copied sources and an owned ledger.
No model credentials, Genesis MCP startup, deployment or live verification.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / "src"), str(SOURCE / "scripts")]

from codex_validator_pilot import RECIPES, source_paths  # noqa: E402
from codex_validator_serving import VALID_BRACKET  # noqa: E402

from genesis.eval.qualification.evidence import canonical, digest  # noqa: E402
from tests.test_hooks.native_codex import fixture_provider  # noqa: E402
from tests.test_scripts.test_pr_verification_closer import _doc_for, _seed  # noqa: E402


def command_item(command: str, index: int) -> dict:
    return {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
            "name": "exec_command", "arguments": json.dumps({"cmd": command, "yield_time_ms": 30000})}


def patch_item(path: Path, value: dict, index: int) -> dict:
    patch = f"*** Begin Patch\n*** Add File: {path}\n+{json.dumps(value)}\n*** End Patch"
    return {"type": "custom_tool_call", "id": f"ct_{index}", "call_id": f"call_{index}",
            "name": "apply_patch", "input": patch}


def ledger_snapshot(path: Path) -> dict:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT * FROM pr_verifications ORDER BY pr_number").fetchall()
    return {"rows": rows, "hashes": {
        suffix: hashlib.sha256(path.with_name(path.name + suffix).read_bytes()).hexdigest()
        for suffix in ("", "-wal")
    }}


def wait_for_pytest():
    # Wait outside model entry and deployment locking; never bypass other tests.
    deadline = time.monotonic() + 900
    with open(Path.home() / ".genesis/locks/pytest.lock") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Qualification waited for a peer test holder") from None
                time.sleep(5)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
                return


def process_state(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"state": fields[0], "parent": int(fields[1]), "start": fields[19],
                "command": Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def cancel_native_probe(process, root, evidence):
    marker = root / "held-pytest.json"
    deadline = time.monotonic() + 900
    while not marker.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Native cancellation did not reach the held recipe")
        time.sleep(1)
    job = json.loads(marker.read_text())["pid"]
    governor = next(
        int(path.name) for path in Path("/proc").iterdir()
        if path.name.isdecimal()
        and (state := process_state(int(path.name)))
        and str(root) in state["command"] and "scripts/codex_validator_job.py" in state["command"]
    )
    owned_governor = process_state(governor)
    try:
        observe_native_cancel(process, root, evidence, governor, owned_governor, job)
    finally:
        # Protect setup and assertion failures as well as the stop observation.
        current = process_state(governor)
        if (owned_governor and current and current["state"] != "Z"
                and current["start"] == owned_governor["start"]
                and str(root) in current["command"]):
            os.kill(governor, signal.SIGTERM)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                current = process_state(job)
                if current is None or current["state"] == "Z":
                    break
                time.sleep(1)


def observe_native_cancel(process, root, evidence, governor, owned_governor, job):
    request = owned_governor["parent"]
    ids = {"launcher": process.pid, "request": request, "governor": governor, "pytest": job}
    before = {name: process_state(pid) for name, pid in ids.items()}
    assert all(value and str(root) in value["command"] for value in before.values())
    with open(Path.home() / ".genesis/locks/pytest.lock") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise AssertionError("Held fixture did not own the real pytest lock")
    evidence["cancellation"] = {"ids": ids, "before": before, "signal": "launcher SIGINT"}
    os.kill(process.pid, signal.SIGINT)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        after = {name: process_state(pid) for name, pid in ids.items()}
        if all(value is None or value["state"] == "Z" or value["start"] != before[name]["start"]
               for name, value in after.items()):
            break
        time.sleep(1)
    evidence["cancellation"]["after"] = after
    assert all(value is None or value["state"] == "Z" or value["start"] != before[name]["start"]
               for name, value in after.items()), after
    with open(Path.home() / ".genesis/locks/pytest.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
    evidence["cancellation"]["pytest_lock_released"] = True


def run_launcher(command, env, timeout, *, cancellation=None):
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        if cancellation is not None:
            cancel_native_probe(process, *cancellation)
        out, err = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, out, err)
    finally:
        if process.poll() is None:
            # ALL failed qualifications allow the launcher's async cleanup.
            os.kill(process.pid, signal.SIGINT)
            try:
                process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)


def catalog(body):
    tools = list(body.get("tools", []))
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools.extend(item.get("tools", []))
    names = set()

    def walk(tool, prefix=""):
        if tool.get("type") == "namespace":
            names.add(prefix + tool["name"])
            for child in tool.get("tools", []):
                walk(child, prefix + tool["name"] + ".")
        elif "name" in tool:
            names.add(prefix + tool["name"])
        else:
            names.add(prefix + tool.get("type", "unknown"))

    for tool in tools:
        walk(tool)
    return sorted(names)


def qualify(root: Path, case: str) -> dict:
    root.mkdir(mode=0o700)
    real_codex = str(Path(shutil.which("codex")).resolve())
    runtime = root / "runtime's space"
    runtime.mkdir()
    for name in ("scripts", "src", "tests"):
        shutil.copytree(SOURCE / name, runtime / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(SOURCE / "pyproject.toml", runtime / "pyproject.toml")
    doctrine = Path(".claude/skills/validating-merges/SKILL.md")
    (runtime / doctrine).parent.mkdir(parents=True)
    shutil.copyfile(SOURCE / doctrine, runtime / doctrine)
    paths = set().union(*(source_paths(recipe) for recipe in RECIPES))
    paths.add("tests/test_hooks/native_codex.py")
    hashes = {path: hashlib.sha256((SOURCE / path).read_bytes()).hexdigest() for path in sorted(paths)}
    for path, value in hashes.items():
        assert hashlib.sha256((runtime / path).read_bytes()).hexdigest() == value
    interpreter = runtime / ".venv/bin"
    interpreter.mkdir(parents=True)
    python_wrapper = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n'
    hold_plugin = None
    if case == "cancel":
        hold_plugin = f'''import json,os,pathlib,time
def pytest_sessionstart(session):
 p=pathlib.Path({str(root / 'held-pytest.json')!r}); t=p.with_suffix('.pending')
 t.write_text(json.dumps({{"pid":os.getpid()}})); t.rename(p)
 time.sleep(180)
'''
        (runtime / "src/fixture_hold_plugin.py").write_text(hold_plugin)
        python_wrapper = f'''#!{sys.executable}
import os,sys
arguments=sys.argv[1:]
if arguments[:2]==["-m","pytest"]: arguments[2:2]=["-p","fixture_hold_plugin"]
os.execve({sys.executable!r},[{sys.executable!r},*arguments],dict(os.environ))
'''
    for name in ("python", "python3"):
        (interpreter / name).write_text(python_wrapper)
        (interpreter / name).chmod(0o700)
    commit, token = "1" * 40, "b1-" + "a" * 24
    status = f'''if [ "$2" = "--verify" ]; then bracket={shlex.quote(VALID_BRACKET)}; else bracket={shlex.quote(token)}; fi
printf 'serving: %s\\nhead: %s\\nmainpid: 321\\ninvocation: %s\\nruntime-edits: none\\nruntime-overrides: none\\nbracket: %s\\n' {commit} {commit} {'3' * 32} "$bracket"
'''
    (runtime / "scripts/deploy_code_only.sh").write_text(status)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    for name in ("requests", ".codex", ".codex/receipts"):
        (workspace / name).mkdir(mode=0o700)
    (workspace / ".codex/operation.lock").touch(mode=0o600)
    ledger = root / "owned-ledger.db"
    rows = [
        {"pr": 7 + index, "repo": "owner/repo", "merge_commit": "2" * 40, "recipe": recipe,
         "intent": "Disposable fixture qualification only",
         "sources": {path: hashes[path] for path in source_paths(recipe)}}
        for index, recipe in enumerate(RECIPES)
    ]
    config = {"version": 1, "runtime_commit": commit, "ledger": str(ledger), "rows": rows}
    (workspace / ".codex/pilot.json").write_bytes(canonical(config))
    for row in rows:
        _seed(ledger, pr=row["pr"])
    producer = subprocess.run([
        sys.executable, "-c", "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
        "c.execute(\"UPDATE pr_verifications SET pr_title='owned WAL fixture'\"); c.commit(); os._exit(0)",
        str(ledger),
    ], capture_output=True, timeout=20)
    assert producer.returncode == 0, producer.stderr
    before = ledger_snapshot(ledger)
    actions = []

    def request_pair(value):
        path = workspace / "requests" / f"00000000-0000-0000-0000-{len(actions):012d}.json"
        actions.append(lambda n: patch_item(path, value() if callable(value) else value, n))
        command = shlex.join([str(interpreter / "python"), "-I", str(runtime / "scripts/codex_validator_request.py"),
                              "--workspace-root", str(workspace), "--request", str(path)])
        actions.append(lambda n: command_item(command, n))

    request_pair({"version": 1, "operation": "pilot_packet"})
    for row in rows:
        request_pair({"version": 1, "operation": "pilot_probe", "pr": row["pr"]})

        def preview_payload(row=row):
            receipt_path = workspace / ".codex/receipts" / f"{row['pr']}.json"
            deadline = time.monotonic() + 900
            while not receipt_path.exists():
                assert time.monotonic() < deadline, "fixed probe did not publish a receipt"
                time.sleep(1)
            receipt = json.loads(receipt_path.read_text())
            # The failure arm is a named synthetic canary, not a Genesis defect.
            doc = _doc_for("fail-intent" if row["pr"] == 8 else "pass-mechanical", merge_commit=row["merge_commit"], pr=row["pr"])
            doc["deploy"]["detail"] = "Named synthetic serving fixture; no live deployment proof"
            doc["claims"][0].update(
                claim="Fixture-only preview outcome canary" if row["pr"] == 8 else "Fixed disposable recipe census",
                measurement="Synthetic canary 1 == 2 evaluated false; not a production defect" if row["pr"] == 8 else ", ".join(receipt["cases"]),
            )
            doc["controls"] = []
            return {"version": 1, "operation": "pilot_preview", "pr": row["pr"],
                    "receipt": digest(receipt), "evidence": doc,
                    "note": "Fixture-only outcome; no live census" if row["pr"] in (8, 9) else None,
                    "park": row["pr"] == 9}

        request_pair(preview_payload)
    request_pair({"version": 1, "operation": "pilot_preview", "pr": 7, "dry_run": False})
    actions.append(lambda n: command_item("printf fixture > " + shlex.quote(str(workspace / "raw-canary")), n))
    original_policy = workspace / ".codex/client/config.toml"
    actions.append(lambda n: {"type": "custom_tool_call", "id": f"ct_{n}", "call_id": f"call_{n}",
                               "name": "apply_patch", "input": f"*** Begin Patch\n*** Update File: {original_policy}\n@@\n+bad = true\n*** End Patch"})
    actions.append(lambda n: {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                               "name": "mcp__genesis_validator_health__bootstrap_manifest", "arguments": "{}"})

    def provider(body, number):
        if case == "cancel" and number > 4:
            deadline = time.monotonic() + 900
            while not (root / "launcher-cancelled").exists() and time.monotonic() < deadline:
                time.sleep(1)
        if case in {"workflow", "cancel"} and number <= (4 if case == "cancel" else len(actions)):
            return [actions[number - 1](number)]
        return [{"type": "message", "id": f"msg_{number}", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Fixture completed; no live verification", "annotations": []}]}]

    evidence = {"fixture_only": True, "case": case, "source_hashes": hashes,
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "synthetic_failure_canary": {"expression": "1 == 2", "observed": 1 == 2},
                "status_fixture_sha256": hashlib.sha256(status.encode()).hexdigest(),
                "interpreter_fixture_sha256": hashlib.sha256(python_wrapper.encode()).hexdigest()}
    evidence["ledger_before"] = before
    if hold_plugin is not None:
        evidence["held_fixture_plugin_sha256"] = hashlib.sha256(hold_plugin.encode()).hexdigest()
    try:
        with fixture_provider(provider) as (url, requests):
            evidence["requests"] = requests
            bin_dir = root / "fixture-bin"
            bin_dir.mkdir()
            overrides = ["-c", 'model_provider="fixture"', "-c", "model_providers.fixture=" + json.dumps({
                "name": "fixture", "base_url": url, "wire_api": "responses", "requires_openai_auth": False,
                "supports_websockets": False, "request_max_retries": 0, "stream_max_retries": 0,
                "stream_idle_timeout_ms": 1200000,
            }).replace(": ", " = ")]
            wrapper = f'''#!{sys.executable}
import json,os,pathlib,sys
arguments=sys.argv[1:]
if "exec" in arguments:
 arguments[arguments.index("exec"):arguments.index("exec")]={overrides!r}
 with pathlib.Path({str(root / 'fixture-exec.jsonl')!r}).open('a') as log: log.write(json.dumps(arguments)+'\\n')
os.execve({real_codex!r},[{real_codex!r},*arguments],dict(os.environ))
'''
            binary = bin_dir / "codex"
            binary.write_text(wrapper)
            binary.chmod(0o700)
            evidence["binary_fixture_sha256"] = hashlib.sha256(wrapper.encode()).hexdigest()
            env = {"PATH": str(bin_dir) + ":" + os.environ["PATH"], "HOME": str(Path.home())}
            launcher = [sys.executable, str(runtime / "scripts/codex_validator_terminal.py")]
            prepared = run_launcher([*launcher, "prepare", "--workspace-root", str(workspace)], env, 120)
            evidence["prepare"] = {"exit": prepared.returncode, "stdout": prepared.stdout, "stderr": prepared.stderr}
            assert prepared.returncode == 0, evidence["prepare"]
            policy_before = original_policy.read_bytes()
            wait_for_pytest()
            result = run_launcher([*launcher, "run", "--workspace-root", str(workspace)], env, 1800,
                                  cancellation=(root, evidence) if case == "cancel" else None)
            if case == "cancel":
                (root / "launcher-cancelled").touch()
            evidence["run"] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            assert result.returncode == (2 if case == "cancel" else 0), evidence["run"]
            assert original_policy.read_bytes() == policy_before
        after = ledger_snapshot(ledger)
        assert before == after
        assert not (workspace / "raw-canary").exists()
        names = catalog(requests[0])
        evidence["tool_catalog"] = names
        assert names
        assert not any(any(part in name for part in ("collaboration", "mcp__", "js_repl", "browser", "computer")) for name in names)
        receipts = list((workspace / ".codex/receipts").glob("*.json"))
        if case == "workflow":
            assert len(receipts) == 3
            for row in rows:
                receipt = json.loads((workspace / ".codex/receipts" / f"{row['pr']}.json").read_text())
                assert len(receipt["cases"]) == sum(RECIPES[row["recipe"]][0].values())
            for verdict in ("pass-with-measured-gaps", "fail-intent", "cannot-verify"):
                assert verdict in evidence["run"]["stdout"], verdict
            assert "Validator request refused" in evidence["run"]["stdout"]
            assert len(requests) == len(actions) + 1
        elif case == "noop":
            assert not receipts and len(requests) == 1
        else:
            assert not receipts and evidence["cancellation"]["pytest_lock_released"]
        for path, value in hashes.items():
            assert hashlib.sha256((SOURCE / path).read_bytes()).hexdigest() == value
        evidence.update(passed=True, ledger_before=before, ledger_after=after)
        return evidence
    finally:
        evidence["ledger_after"] = ledger_snapshot(ledger)
        (root / "receipt.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", choices=("workflow", "noop", "cancel"), required=True)
    args = parser.parse_args()
    os.umask(0o077)
    qualify(args.root, args.case)
    print(args.case + " PASS (disposable fixtures only)")
