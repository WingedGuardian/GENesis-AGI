"""Pinned monolith with an explicit native inner invocation; contained CI only."""

import asyncio
import contextlib
import contextvars
import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite
import ego_owned_processes as processes
import headlong_native_controls as native
from ego_runtime_cases import CONFLICTS, FAULTS, SCENARIOS, TWO_WAKES, kind_for, manifest
from headlong_controls import dependency_inventory, environment, preflight, read_jsonl, tree_hash
from headlong_native_bridge import ReceiptView, wake_matches
from headlong_native_provider import Backend

REFERENCE = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"
CROSSING = "aaaaaaaa-1234-4567-8912-bbbbbbbb4111"
MARKERS = ("fixture-identity-marker", "fixture-goal-marker", "fixture-skill-marker", "fixture-memory-marker", "fixture-stream-marker")
MENTION_CONTENT = "fixture-mention-content-should-not-expand"
RECEIPT = "fixture receipt proposal=proposal effect=" + hashlib.sha256(native.EFFECT).hexdigest()
_COMMAND_RECORDS = contextvars.ContextVar("headlong_command_records", default=None)


@contextlib.contextmanager
def command_records():
    """Collect command closure in this task without changing shared callers."""
    records = []
    token = _COMMAND_RECORDS.set(records)
    try:
        yield records
    finally:
        _COMMAND_RECORDS.reset(token)


def closed_commands(records):
    fields = {"argv", "pgid", "returncode", "timed_out", "group_closed", "probe_error"}
    return bool(records) and all(
        isinstance(row, dict) and set(row) == fields
        and isinstance(row["argv"], list) and bool(row["argv"])
        and all(isinstance(arg, str) and bool(arg) for arg in row["argv"])
        and type(row.get("pgid")) is int and row["pgid"] > 1
        and type(row.get("returncode")) is int and row.get("timed_out") is False
        and row.get("group_closed") is True and row.get("probe_error") is None
        for row in records)


def record_command(process, argv, result):
    records = _COMMAND_RECORDS.get()
    if records is None:
        return
    record = {"argv": list(argv), "pgid": process.pid, "returncode": result["returncode"],
              "timed_out": result["timed_out"], "group_closed": False, "probe_error": None}
    try:
        record["group_closed"] = not processes.group_alive(process.pid)
        if not record["group_closed"]:
            # The fresh Popen created this session/group. A later successful
            # cleanup never changes the recorded failure into a clean command.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    except OSError as exc:
        record["probe_error"] = type(exc).__name__
    records.append(record)


def patched_source(source, patch):
    for prefix in ("", "prompt_"):
        if prefix + "before" in patch:
            before, after = patch[prefix + "before"], patch[prefix + "after"]
            if source.count(before) != 1:
                raise ValueError("expected exactly one pinned adaptation region")
            source = source.replace(before, after)
    return source


def captured_text(backend):
    return "\n".join(row["content"] if isinstance(row["content"], str) else
        "\n".join(block.get("text", "") for block in row["content"])
        for capture in backend["captures"] for row in capture["body"]["messages"])


def qualify_run(name, row, second=False):
    if name in CONFLICTS:
        return qualify_conflict(name, row)
    try:
        driver, backend = row["driver"], row["backend"]
        result, calls, run_id = driver["native"], driver["calls"], driver["run_id"]
        kind = kind_for(name, second)
        effected = name in {"effect", "large_context", *TWO_WAKES, *FAULTS}
        visible = effected and not second and name != "observation_failure"
        failed = name in {"provider_error", "handler_error", *FAULTS}
        count = 0 if kind in {"echo", "provider_error"} else 1
        expected_call = ({"canonical_called": True, "claim": None, "outcome": "error"} if name == "handler_error" else
                         {"canonical_called": True, "claim": True, "outcome": "error"} if name == "observation_failure" else
                         {"canonical_called": True, "claim": effected and not second,
                          "outcome": "executed" if effected and not second else "refused"})
        linked = [step for step in row["trajectory"] if step.get("run_id") == run_id]
        headers = [step for step in row["trajectory"] if step.get("step_id") == run_id]
        grouped = {kind: [step for step in linked if step["type"] == kind] for kind in ("observation", "final", "idle", "error")}
        expected_delay = 0 if visible or name == "reactive_noop" else 5
        text = captured_text(backend)
        checks = [row["step_returncode"] == 0, not row.get("parse_errors"),
            row["prompt_captured"] is True,
            driver["prompt_status"] == "0", driver["failure_kind"] is None,
            driver["invocation_id"] == row["invocation_id"] == headers[0]["invocation_id"],
            driver["trigger_id"] == row["trigger"]["step_id"],
            backend["main_requests"] > 0, not backend["unexpected"], row["raw_prompt"] in text,
            driver["prompt_sha256"] == hashlib.sha256(row["raw_prompt"].encode()).hexdigest(),
            all(marker in row["raw_prompt"] for marker in MARKERS if not second or marker != "fixture-memory-marker"),
            "@mention-target.txt" in text, MENTION_CONTENT not in text,
            len(calls) == count, calls == row["receipt_calls"],
            sorted(backend["tool_results"]) == (["toolu_fixture_1"] if count else []),
            (row["state"]["status"] == "executed") if effected else row["state"]["status"] == "pending",
            row["effect"] == (native.EFFECT.decode() if effected else None),
            driver["complete"] is (not failed), result is not None and bool(result["session_id"]),
            row["backoff"]["level"] == (0 if expected_delay == 0 else 1),
            row["start"] - 1 <= row["wake_mtime"] <= row["end"] + 1,
            wake_matches(row["wake_at"], row["wake_mtime"], expected_delay),
            len(headers) == 1, headers[0]["type"] == "shellm-run", headers[0]["engine"] == "claude-code-sdk",
            driver["model"] == headers[0]["model"] == "sonnet",
            headers[0]["source"] == "monolith", headers[0]["trigger_step"] == row["trigger"]["step_id"],
            headers[0]["wake"] == ("reactive/observation:fixture" if name == "reactive_noop" else "spontaneous/timer"),
            len(grouped["observation"]) == int(visible), len(grouped["final"]) == int(not failed),
            len(grouped["idle"]) == int(not failed and not visible),
            len(grouped["error"]) == int(failed and not visible),
            all(step.get("source") == "monolith" for step in linked),
            any(message["type"] == "ResultMessage" and message["data"]["session_id"] == result["session_id"] for message in driver["messages"]),
            any(message["type"] == "SystemMessage" and message["data"]["subtype"] == "init"
                and message["data"]["data"]["tools"] == ["mcp__fixture__claim_effect"] for message in driver["messages"])]
        if count:
            checks.append(all(calls[0][key] == value for key, value in expected_call.items()))
        if kind == "provider_error":
            checks.extend([backend["provider_error"] is True, result["is_error"] is True, result["api_error_status"] == 400])
        else:
            checks.extend([result["is_error"] is False, result["subtype"] == "success", result["result"] == f"native-final-{kind}"])
        if visible:
            checks.extend([grouped["observation"][0]["step_id"] == driver["observation_id"], grouped["observation"][0]["content"] == RECEIPT])
        if not failed:
            checks.extend([grouped["final"][0]["step_id"] == driver["final_id"],
                           grouped["final"][0]["native_session_id"] == result["session_id"],
                           grouped["final"][0]["content"] == (RECEIPT + "\n" if visible else "") + result["result"]])
        else:
            checks.append(driver["final_id"] is None)
        if name == "handler_error":
            checks.append("synthetic handler-error" in calls[0].get("error", ""))
        if name == "observation_failure":
            checks.append("synthetic observation append failure" in calls[0].get("error", ""))
        if name == "final_failure":
            checks.append("synthetic final append failure" in driver["error"])
        if name == "large_context":
            checks.extend([len(row["raw_prompt"].encode()) > 131072, "λ" in row["raw_prompt"],
                           "continuity context " * 9000 in row["raw_prompt"]])
        if second:
            checks.extend([RECEIPT in row["raw_prompt"], f"run_id={row['previous_run_id']}" in row["raw_prompt"],
                           result["session_id"] != row["previous_session_id"]])
        if name == "reference_continuity":
            if not second:
                checks.append(run_id == REFERENCE)
            else:
                lookup = row["history_lookup"]
                checks.extend([row["previous_run_id"] == REFERENCE, lookup["returncode"] == 0,
                    len(lookup["rows"]) == 2, all(step.get("run_id") == REFERENCE for step in lookup["rows"]),
                    {step["step_id"] for step in lookup["rows"]} == {row["previous_observation_id"], row["previous_final_id"]}])
        return all(checks)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


def qualify_conflict(name, row):
    try:
        driver, backend = row["driver"], row["backend"]
        run_id = driver["run_id"]
        headers = [step for step in row["trajectory"] if step.get("step_id") == run_id]
        linked = [step for step in row["trajectory"] if step.get("run_id") == run_id]
        delay = 0 if name == "reactive_reference_conflict" else 5
        return all((row["step_returncode"] == 0, not row.get("parse_errors"), bool(run_id), len(headers) == 1,
            headers[0]["type"] == "shellm-run", headers[0]["engine"] == "claude-code-sdk",
            headers[0]["model"] == driver["model"] == "sonnet",
            headers[0]["source"] == "monolith", headers[0]["trigger_step"] == row["trigger"]["step_id"],
            headers[0]["invocation_id"] == driver["invocation_id"] == row["invocation_id"],
            driver["trigger_id"] == row["trigger"]["step_id"],
            headers[0]["wake"] == ("reactive/observation:fixture" if delay == 0 else "spontaneous/timer"),
            driver["complete"] is False, driver["native"] is None,
            driver["prompt_status"] == "65", driver["failure_kind"] == "reference_conflict",
            driver["error"] == "ValueError: reference_conflict", driver["calls"] == row["receipt_calls"] == [],
            driver["messages"] == driver["stderr"] == [], driver["observation_id"] is driver["final_id"] is None,
            backend["main_requests"] == 0, backend["tool_results"] == backend["unexpected"] == backend["captures"] == [],
            row["raw_prompt"] == "", row["effect"] is None, row["state"] == row["state_before"], row["state"]["status"] == "approved",
            row["prompt_captured"] is True, driver["prompt_sha256"] == hashlib.sha256(b"").hexdigest(),
            backend["provider_error"] is False,
            len(linked) == 1, linked[0]["type"] == "error", linked[0]["rc"] == 2, linked[0]["reason"] == "run-failed",
            linked[0]["source"] == "monolith", linked[0]["trigger_step"] == row["trigger"]["step_id"],
            row["backoff"]["level"] == (0 if delay == 0 else 1),
            row["start"] - 1 <= row["wake_mtime"] <= row["end"] + 1,
            wake_matches(row["wake_at"], row["wake_mtime"], delay)))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


def json_artifact(path, errors):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return None


def command(argv, env, root, data=None, timeout=100):
    with subprocess.Popen(argv, env=env, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # A writer outside the outer group can keep a pipe open. Retain
                # diagnostics and refuse qualification instead of hanging here.
                stdout, stderr = exc.output or b"", exc.stderr or b""
                stdout = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
                stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
                # Reap the SIGKILLed leader before the group probe below: an unreaped
                # zombie is still a group member, so the probe would record a closed
                # group as alive. SIGKILL cannot be ignored, so this wait ends.
                process.wait()
        result = {"returncode": -9 if timed_out else process.returncode,
                  "stdout": stdout, "stderr": stderr, "timed_out": timed_out}
        record_command(process, argv, result)
    return result


def append(record, env, root):
    result = command(["/headlong-b/bin/traj", "append"], env, root, json.dumps(record), timeout=10)
    if result["returncode"]:
        raise ValueError(result)
    return result["stdout"].strip()


async def run_scenario(root, name):
    with command_records() as commands:
        result = await _run_scenario(root, name)
    result.update(commands=commands, cleanup_confirmed=closed_commands(commands))
    result["control_passed"] = result["control_passed"] and result["cleanup_confirmed"]
    return result


async def _run_scenario(root, name):
    from headlong_prompt_references import canonical_id, publish_config

    root.mkdir()
    env = environment(root)
    identity = root / "identity"
    for subdir in ("run", "workdir", "memories", "skills/fixture", "kernel", "trajectories"):
        (identity / subdir).mkdir(parents=True, exist_ok=True)
    env.update(PATH="/headlong-b/bin:/headlong-b/tools:/usr/local/bin:/usr/bin:/bin", PYTHONPATH="/genesis-src", IDENTITY_NAME="fixture",
        IDENTITY_DIR=str(identity), MEM_DIR=str(identity / "memories"), TRAJ_DIR=str(identity / "trajectories"),
        SKILLS_DIR=str(identity / "skills"), SKILLS_KERNEL_DIR=str(identity / "kernel"), THINK_MODEL="sonnet", SHELLM_MODEL="sonnet",
        MONOLITH_TIERED_MEMORY="0", MONOLITH_BACKOFF_BASE="5", MONOLITH_BACKOFF_FACTOR="2", MONOLITH_BACKOFF_CAP="40",
        MONOLITH_BACKOFF_HOLD="1", MONOLITH_RELATED_MEMORIES="3")
    created = command(["/headlong-b/bin/traj", "new", "--traj_dir", env["TRAJ_DIR"], "--slug", "fixture"], env, root, timeout=10)
    if created["returncode"]:
        raise ValueError(created)
    env["TRAJ_ID"] = canonical_id(created["stdout"].splitlines()[0])
    trajectory, = (identity / "trajectories").rglob("trajectory.jsonl")
    (identity / "info.txt").write_text(f"name=fixture\nroot_trajectory={env['TRAJ_ID']}\n")
    (identity / "run/dispatcher.token").write_text("synthetic-not-a-real-dispatcher")
    (identity / "core_identity_prompt.md").write_text("fixture-identity-marker λ @mention-target.txt\n" +
        ("continuity context " * 9000 if name == "large_context" else ""))
    (identity / "workdir/mention-target.txt").write_text(MENTION_CONTENT)
    (identity / "memories/goal.md").write_text("---\ntype: goal\ncreated: 2000-01-01\n---\nfixture-goal-marker\n")
    (identity / "memories/continuity-note.md").write_text("---\ntype: memory\ncreated: 2000-01-01\n---\ncontinuity orbit fixture-memory-marker\n")
    (identity / "skills/fixture/SKILL.md").write_text("---\nname: fixture\ndescription: fixture-skill-marker\n---\nSynthetic context only.\n")
    issued = [env["TRAJ_ID"], canonical_id(append({"type": "observation", "source": "fixture",
                                               "content": "continuity orbit fixture-stream-marker"}, env, root))]
    if name in CONFLICTS:
        issued.append(canonical_id(append({"type": "observation", "source": "fixture", "step_id": CROSSING,
                                           "content": "owned crossing control"}, env, root)))
        with (identity / "core_identity_prompt.md").open("a") as stream:
            stream.write(CROSSING + " 1111 1111\n")
    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        for table in ("ego_proposals", "ego_proposal_revisions"):
            await db.execute(native.TABLES[table])
        await native.prepare_case(db, "approved" if name in {"effect", "large_context", *TWO_WAKES, *FAULTS, *CONFLICTS} else "pending")
        state_before = await native.ego.get_proposal(db, "proposal")
    runs = []
    for index in range(2 if name in TWO_WAKES else 1):
        kind = kind_for(name, second=bool(index))
        run_root = root / f"run-{index}"
        run_root.mkdir()
        reactive = name in {"reactive_noop", "reactive_reference_conflict"}
        trigger = {"type": "observation" if reactive else "monolith-wake",
                   "source": "fixture" if reactive else "monolith-timer", "content": "continuity orbit wake"}
        trigger["step_id"] = canonical_id(append(trigger, env, root))
        issued.append(trigger["step_id"])
        with Backend(kind, ReceiptView(run_root / "calls.jsonl")) as backend:
            config = {"root": str(root), "run_root": str(run_root), "identity_dir": str(identity), "model": "sonnet",
                      "traj_id": env["TRAJ_ID"], "kind": kind, "backend_port": backend.server.server_port,
                      "scenario": name, "trigger_id": trigger["step_id"], "reference_ids": sorted(set(issued)),
                      "fixture_header_id": REFERENCE if name == "reference_continuity" and index == 0 else None,
                      "fault": "observation" if name == "observation_failure" else "final" if name == "final_failure" else None}
            child_env = publish_config(config, env)
            start = time.time()
            result = command(["/headlong-b/thinkers/monolith/step"], child_env, root,
                             json.dumps(trigger))
            end = time.time()
        errors = []
        async with aiosqlite.connect(f"file:{root / 'fixture.sqlite3'}?mode=ro", uri=True) as reader:
            reader.row_factory = aiosqlite.Row
            state = await native.ego.get_proposal(reader, "proposal")
        wake = identity / "run/monolith.wake_at"
        try:
            receipt_calls = ReceiptView(run_root / "calls.jsonl").calls
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            receipt_calls = None
        row = {"driver": json_artifact(run_root / "driver.json", errors), "backend": backend.evidence, "receipt_calls": receipt_calls,
               "prompt_captured": (run_root / "raw-prompt.bin").is_file() and (run_root / "raw-prompt.txt").is_file(),
               "state_before": state_before, "invocation_id": child_env["HEADLONG_REFERENCE_NONCE"],
               "raw_prompt": (run_root / "raw-prompt.bin").read_bytes().decode() if (run_root / "raw-prompt.bin").exists() else "",
               "trajectory": read_jsonl(trajectory), "state": state, "effect": (root / "effect.bin").read_text() if (root / "effect.bin").exists() else None,
               "backoff": json_artifact(identity / "run/monolith_backoff_state.json", errors), "wake_at": int(wake.read_text()) if wake.exists() else None,
               "wake_mtime": wake.stat().st_mtime if wake.exists() else None, "start": start, "end": end, "trigger": trigger,
               "step_returncode": result["returncode"], "stdout": result["stdout"], "stderr": result["stderr"], "parse_errors": errors}
        if index:
            previous = runs[0]["driver"]
            lookup = command(["/headlong-b/bin/traj", "tail", "-n", "400", "--filter", "run_id=" + previous["run_id"]], env, root)
            try:
                lookup_rows = [json.loads(line) for line in lookup["stdout"].splitlines()]
            except ValueError as exc:
                errors.append(str(exc))
                lookup_rows = []
            row.update(previous_run_id=previous["run_id"], previous_session_id=previous["native"]["session_id"],
                       previous_observation_id=previous["observation_id"], previous_final_id=previous["final_id"],
                       history_lookup={"returncode": lookup["returncode"], "rows": lookup_rows})
        row["control_passed"] = qualify_run(name, row, second=bool(index))
        runs.append(row)
        if not row["control_passed"]:
            break
        if name in TWO_WAKES and index == 0:
            if not row["driver"]["complete"]:
                raise ValueError("incomplete native receipt cannot register references")
            issued.extend(canonical_id(row["driver"][key]) for key in ("run_id", "observation_id", "final_id"))
    return {"scenario": name, "runs": runs, "control_passed": len(runs) == (2 if name in TWO_WAKES else 1) and all(row["control_passed"] for row in runs)}


def correlation_controls(root):
    with command_records() as commands:
        result = _correlation_controls(root)
    result.update(commands=commands, cleanup_confirmed=closed_commands(commands))
    result["passed"] = result["passed"] and result["cleanup_confirmed"]
    return result


def _correlation_controls(root):
    """Actual readers on disposable records; no responder model or delivery process."""
    root.mkdir()
    env = environment(root)
    identity = root / "identity"
    (identity / "trajectories").mkdir(parents=True)
    env.update(IDENTITY_NAME="fixture", IDENTITY_DIR=str(identity), TRAJ_DIR=str(identity / "trajectories"),
               PATH="/headlong-b/bin:/headlong-b/tools:/usr/local/bin:/usr/bin:/bin")
    created = command(["traj", "new", "--slug", "joins"], env, root)
    if created["returncode"]:
        raise ValueError("correlation trajectory creation failed")
    env["TRAJ_ID"] = created["stdout"].splitlines()[0]
    inbound = append({"type": "message", "step_id": REFERENCE, "from": "fixture-person", "to": "fixture",
                      "content": "synthetic request"}, env, root)
    append({"type": "action", "source": "responder", "trigger_step": inbound,
            "person": "fixture-person", "request": "synthetic deferral"}, env, root)
    before = command(["chat", "pending", "--json"], env, root)
    reply = command(["chat", "reply", "--reply-to", inbound, "fixture-person", "synthetic first response"], env, root)
    duplicate = command(["chat", "reply", "--reply-to", inbound, "fixture-person", "synthetic second response"], env, root)
    append({"type": "observation", "source": "fixture", "resolves": inbound, "content": "synthetic resolution"}, env, root)
    after = command(["chat", "pending", "--json"], env, root)
    trajectory, = (identity / "trajectories").rglob("trajectory.jsonl")
    messages = [step for step in read_jsonl(trajectory) if step.get("type") == "message" and step.get("from") == "fixture"]
    pending_before, pending_after = json.loads(before["stdout"]), json.loads(after["stdout"])
    passed = (inbound == REFERENCE and all(row["returncode"] == 0 for row in (before, reply, duplicate, after))
              and len(pending_before) == 1 and pending_before[0]["trigger_step"] == REFERENCE and pending_after == []
              and len(messages) == 1 and messages[0]["reply_to"] == REFERENCE and messages[0]["content"] == "synthetic first response")
    return {"passed": passed, "pending_before": pending_before, "pending_after": pending_after,
            "messages": messages, "returncodes": [row["returncode"] for row in (before, reply, duplicate, after)]}


async def main():
    preflight()
    import claude_agent_sdk
    from headlong_reference_patch import adapt_common

    init = processes.init_identity(controller="/fixture/headlong_monolith_controls.py")
    before = processes.census()
    if set(before) != {1, os.getpid()}:
        raise processes.OwnershipError("unexpected process in fresh B container")
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled/claude"
    with command_records() as version_commands:
        version = command([str(cli), "--version"], os.environ.copy(), Path("/work"), timeout=10)
    if version["returncode"] or version["timed_out"]:
        raise ValueError("native version command failed")
    binary = {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
              "version": version["stdout"].strip()}
    patch = json.loads(Path("/fixture/headlong_bridge_patch.json").read_text())
    if set(patch) != {"before", "after", "prompt_before", "prompt_after"}:
        raise ValueError("incomplete bridge adaptation specification")
    step = "thinkers/monolith/step"
    if Path("/headlong-b", step).read_text() != patched_source(Path("/headlong", step).read_text(), patch):
        raise ValueError("unexpected monolith adaptation")
    common = "thinkers/_lib/common.sh"
    if Path("/headlong-b", common).read_text() != adapt_common(Path("/headlong", common).read_text()):
        raise ValueError("unexpected reference adaptation")
    sources = {name: tree_hash(Path(path)) for name, path in {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    with TemporaryDirectory(dir="/work") as directory:
        cases = [await run_scenario(Path(directory) / name, name) for name in SCENARIOS + FAULTS + CONFLICTS]
        joins = correlation_controls(Path(directory) / "correlations")
    unchanged = sources == {name: tree_hash(Path(path)) for name, path in {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    after = processes.census()
    survivors = sorted(set(after) - set(before))
    logs = [version_commands, *[case["commands"] for case in cases], joins["commands"]]
    cleanup = {"init": init, "before": {str(pid): row for pid, row in before.items()},
               "after": {str(pid): row for pid, row in after.items()}, "survivors": survivors}
    stable = set(before) == set(after) and all(
        before[pid][field] == after[pid][field]
        for pid in before for field in ("pid", "ppid", "pgid", "sid", "start"))
    clean = stable and all(closed_commands(commands) for commands in logs)
    passed = unchanged and clean and joins["passed"] and all(case["control_passed"] for case in cases)
    print(json.dumps({"schema": "headlong-native-monolith/v1", "status": "pass" if passed else "incomplete", "cases": cases,
                      "comparison_manifest": manifest(),
                      "sources": sources, "sources_unchanged": unchanged, "cleanup_confirmed": clean,
                      "cleanup": cleanup, "version_commands": version_commands, "correlations": joins,
                      "sdk": importlib.metadata.version("claude-agent-sdk"), "dependencies": dependency_inventory(),
                      "native_binary": binary,
                      "patch_sha256": hashlib.sha256(Path("/fixture/headlong_bridge_patch.json").read_bytes()).hexdigest(),
                      "source_pin": Path("/fixture/genesis-source-pin").read_text().strip(),
                      "profile": "MCP claim_effect only; tiered off, deterministic related context on, verbatim prompt; no full ego/capability/dispatcher parity."}))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "reason": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(2) from exc
