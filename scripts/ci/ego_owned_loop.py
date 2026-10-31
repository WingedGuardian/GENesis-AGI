"""C1 owned outer loop. Execute real native/upstream code only in containment."""

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiosqlite
from ego_owned_state import State
from headlong_controls import preflight, read_jsonl
from headlong_native_bridge import ReceiptView
from headlong_native_controls import EFFECT
from headlong_prompt_references import canonical_id, publish_config

from genesis.db.crud import ego
from genesis.eval.qualification.evidence import Campaign, canonical, load_json
from genesis.util.atomic import atomic_write_text


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_hashes():
    directory = Path(__file__).parent
    return {key: sha((directory / name).read_bytes()) for key, name in {
        "state": "ego_owned_state.py", "loop": "ego_owned_loop.py", "context": "ego_owned_context.sh",
        "bridge": "headlong_native_bridge.py", "cases": "ego_runtime_cases.py"}.items()}


def environment(root, spec):
    identity = root / "identity"
    return {"PATH": "/headlong-b/bin:/headlong-b/tools:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": str(root / "home"), "HEADLONG_HOME": str(root / "state"), "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/genesis-src", "SHELLM_ENV": "local",
            "IDENTITY_NAME": "fixture", "IDENTITY_DIR": str(identity), "MEM_DIR": str(identity / "memories"),
            "TRAJ_DIR": str(identity / "trajectories"), "TRAJ_ID": spec["trajectory_id"],
            "ROOT_TRAJ_ID": spec["trajectory_id"], "SKILLS_DIR": str(identity / "skills"),
            "SKILLS_KERNEL_DIR": str(identity / "kernel"), "THINKER_NAME": "ego-owned-loop",
            "LLM_RETRIES": "0", "LLM_MAX_TIME": "10", "SHELLM_MAX_EXEC_TIME": "10"}


def command(argv, env, cwd, data=None):
    return subprocess.run(argv, env=env, cwd=cwd, input=data, text=True, capture_output=True,
                          timeout=20, check=False)


def append_trigger(event, env, root):
    record = {"type": event["type"], "source": "ego-owned-loop", "content": event["content"],
              "input_event_id": event["id"]}
    result = command(["/headlong-b/bin/traj", "append"], env, root / "identity/workdir", json.dumps(record))
    if result.returncode:
        raise ValueError("actual trigger writer failed")
    identifier = canonical_id(result.stdout.strip())
    return {**record, "step_id": identifier}


def trajectory(root):
    files = list((root / "identity/trajectories").rglob("trajectory.jsonl"))
    if len(files) != 1:
        raise ValueError("expected one owned trajectory")
    return files[0]


async def snapshot(root):
    async with aiosqlite.connect((root / "fixture.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        state = await ego.get_proposal(db, "proposal")
    effect = root / "effect.bin"
    return {"state": state, "effect": effect.read_bytes().hex() if effect.exists() else None}


def save(path, value):
    atomic_write_text(path, canonical(value).decode(), overwrite=False)


def prepare(root, spec, state, journal, attempt):
    run = root / f"attempt-{attempt['number']:05d}"
    run.mkdir()
    env = environment(root, spec)
    trigger = append_trigger(attempt["event"], env, root)
    rows = read_jsonl(trajectory(root))
    if sum(row.get("step_id") == trigger["step_id"] and all(row.get(k) == v for k, v in trigger.items())
           for row in rows) != 1:
        raise ValueError("trigger writer return does not resolve")
    response = spec["responses"][attempt["number"] - 1]
    references = set(spec["reference_ids"]) | {spec["trajectory_id"], trigger["step_id"]}
    for observation in state.observations:
        evidence = observation["evidence"]
        if observation["outcome"] in {"completed", "noop", "refused"}:
            verify_artifacts(root, evidence["artifacts"])
            references.update(value for value in evidence["references"] if value is not None)
    config = {**response, "root": str(root), "run_root": str(run), "identity_dir": str(root / "identity"),
              "model": spec["model"], "traj_id": spec["trajectory_id"], "trigger_id": trigger["step_id"],
              "reference_ids": sorted(references), "outer_source": "ego-owned-loop"}
    env = publish_config(config, env)
    save(run / "trigger.json", trigger)
    raw, related = run / "context.raw", run / "related.txt"
    env.update(OWNED_TRIGGER_FILE=str(run / "trigger.json"), OWNED_RELATED_FILE=str(related),
               OWNED_RELATED_PREV="\n".join(state.related), OWNED_RAW_PROMPT=str(raw))
    rendered = command(["bash", "/fixture/ego_owned_context.sh"], env, root / "identity/workdir")
    if rendered.returncode:
        raise ValueError("owned context rendering failed")
    atomic_write_text(raw, rendered.stdout, overwrite=False)
    redacted = command(["bash", "/fixture/ego_owned_context.sh", "--redact"], env, root / "identity/workdir")
    prompt = root / f"identity/run/monolith_prompt.{attempt['id']}"
    atomic_write_text(prompt, "" if redacted.returncode else redacted.stdout, overwrite=False)
    env.update(HEADLONG_NATIVE_PROMPT_STATUS=str(redacted.returncode), SHELLM_TRIGGER_STEP_ID=trigger["step_id"],
               SHELLM_WAKE="owned-loop/" + attempt["event"]["kind"],
               SHELLM_RUN_ID_OUT=str(run / "native-run-id"))
    binding = {"attempt_id": attempt["id"], "trigger_id": trigger["step_id"],
               "nonce": env["HEADLONG_REFERENCE_NONCE"], "config_sha256": sha((run / "config.json").read_bytes()),
               "prompt_sha256": sha(prompt.read_bytes()), "prompt_status": str(redacted.returncode)}
    state.bind(journal, binding)
    return run, prompt, env, binding


def group_alive(identifier):
    try:
        os.killpg(identifier, 0)
        return True
    except ProcessLookupError:
        return False


async def invoke(argv, env, run, stop):
    if stop.is_set():
        raise RuntimeError("owned child stopped before launch")
    process = await asyncio.create_subprocess_exec(*argv, env=env, cwd=env["IDENTITY_DIR"] + "/workdir",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    work, stopping = asyncio.create_task(process.communicate()), asyncio.create_task(stop.wait())
    try:
        save(run / "child.json", {"pid": process.pid, "pgid": process.pid})
        done, _ = await asyncio.wait({work, stopping}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
        if work not in done:
            raise RuntimeError("owned child interrupted or timed out")
        stdout, stderr = await work
        atomic_write_text(run / "stdout.txt", stdout.decode(), overwrite=False)
        atomic_write_text(run / "stderr.txt", stderr.decode(), overwrite=False)
        return process.returncode
    finally:
        stopping.cancel()
        if group_alive(process.pid):
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.sleep(0.1)
            if group_alive(process.pid):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, stopping, return_exceptions=True)
        if group_alive(process.pid):
            raise RuntimeError("owned child cleanup unconfirmed")
        save(run / "child-closed.json", {"pid": process.pid, "pgid": process.pid,
                                        "returncode": process.returncode, "group_alive": False})


def verify_artifacts(root, artifacts):
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("missing C1 artifact map")
    for item in artifacts:
        path = root / item["path"]
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("misbound C1 artifact")
        data = path.read_bytes()
        if len(data) != item["size"] or sha(data) != item["sha256"]:
            raise ValueError("changed C1 artifact")


async def collect(root, spec, attempt, run, binding, returncode):
    after = await snapshot(root)
    save(run / "canonical-after.json", after)
    rows = read_jsonl(trajectory(root))
    save(run / "trajectory-snapshot.json", rows)
    result = {"outcome": "uncertain", "references": [], "after": after, "errors": []}
    try:
        for name in ("trigger.json", "context.raw", "related.txt", "stdout.txt", "stderr.txt", "native-run-id"):
            if not (run / name).is_file():
                raise ValueError("missing required C1 child artifact: " + name)
        trigger = load_json((run / "trigger.json").read_text())
        # Upstream adds timestamp metadata; compare the retained trigger fields.
        matches = [row for row in rows if row.get("step_id") == binding["trigger_id"]]
        if (trigger["step_id"] != binding["trigger_id"] or len(matches) != 1
                or any(canonical(matches[0].get(k)) != canonical(v) for k, v in trigger.items())):
            raise ValueError("misbound retained C1 trigger")
        if sha((run / "config.json").read_bytes()) != binding["config_sha256"]:
            raise ValueError("changed bound C1 config")
        if any(sha((run / name).read_bytes()) != binding["prompt_sha256"]
               for name in ("raw-prompt.bin", "raw-prompt.txt")):
            raise ValueError("changed bound C1 prompt capture")
        driver = load_json((run / "driver.json").read_text())
        child = load_json((run / "child.json").read_text())
        closed = load_json((run / "child-closed.json").read_text())
        if (type(child["pid"]) is not int or child["pid"] <= 0 or child["pid"] != child["pgid"]
                or canonical(closed) != canonical({**child, "returncode": returncode, "group_alive": False})):
            raise ValueError("unconfirmed C1 child cleanup")
        calls = ReceiptView(run / "calls.jsonl").calls
        native = driver["native"]
        rid = canonical_id(driver["run_id"])
        if (run / "native-run-id").read_text() != rid or type(driver["complete"]) is not bool:
            raise ValueError("misbound C1 run ID or completion type")
        headers = [r for r in rows if r.get("step_id") == rid]
        linked = [r for r in rows if r.get("run_id") == rid]
        if (len(headers) != 1 or headers[0].get("source") != "ego-owned-loop"
                or headers[0].get("engine") != "claude-code-sdk" or driver["outer_source"] != "ego-owned-loop"
                or headers[0].get("model") != spec["model"] or driver["model"] != spec["model"]
                or headers[0].get("wake") != "owned-loop/" + attempt["event"]["kind"]
                or headers[0].get("trigger_step") != binding["trigger_id"] or driver["trigger_id"] != binding["trigger_id"]
                or headers[0].get("invocation_id") != binding["nonce"] or driver["invocation_id"] != binding["nonce"]
                or driver["prompt_sha256"] != binding["prompt_sha256"] or driver["prompt_status"] != binding["prompt_status"]
                or canonical(driver["calls"]) != canonical(calls)
                or any(r.get("source") != "ego-owned-loop" for r in linked)):
            raise ValueError("inconsistent C1 child identity")
        finals = [r for r in linked if r["type"] == "final"]
        observations = [r for r in linked if r["type"] == "observation"]
        if len(linked) != len(finals) + len(observations):
            raise ValueError("unexpected C1 child record type")
        if native is not None:
            terminals = [m["data"] for m in driver["messages"] if m["type"] == "ResultMessage"]
            inits = [m["data"]["data"] for m in driver["messages"]
                     if m["type"] == "SystemMessage" and m["data"]["subtype"] == "init"]
            if (len(terminals) != 1 or canonical(terminals[0]) != canonical(native) or len(inits) != 1
                    or inits[0]["tools"] != ["mcp__fixture__claim_effect"]
                    or inits[0]["session_id"] != native["session_id"]
                    or not isinstance(inits[0]["model"], str) or not inits[0]["model"]):
                raise ValueError("unbound C1 native terminal or tool profile")
            result.update(executor="claude-code", requested_model=spec["model"], resolved_model=inits[0]["model"])
        claims = sum(c["claim"] is True for c in calls)
        before = attempt["before"]
        if any(c["outcome"] in {"started", "error"} for c in calls):
            raise ValueError("unfinished or failed protected handler")
        if any(canonical(c) != canonical({"canonical_called": True, "claim": c["claim"],
                                         "outcome": "executed" if c["claim"] is True else "refused"})
               or type(c["claim"]) is not bool for c in calls):
            raise ValueError("inconsistent C1 protected call disposition")
        if claims:
            expected = {**before["state"], "status": "executed", "user_response": "dispatching"}
            if claims != 1 or before["effect"] is not None or after != {"state": expected, "effect": EFFECT.hex()}:
                raise ValueError("claim, committed state and effect disagree")
        elif canonical(before) != canonical(after):
            raise ValueError("unclaimed effect or state changed")
        if driver["complete"] is True:
            if (returncode != 0 or not native or native["is_error"] is not False or native["subtype"] != "success"
                    or not native["session_id"] or len(finals) != 1 or len(observations) != claims
                    or finals[0]["step_id"] != driver["final_id"]
                    or finals[0]["native_session_id"] != native["session_id"]
                    or (claims and observations[0]["step_id"] != driver["observation_id"])):
                raise ValueError("incomplete C1 native terminal evidence")
            canonical_id(driver["final_id"])
            if claims:
                canonical_id(driver["observation_id"])
            receipt = "fixture receipt proposal=proposal effect=" + sha(EFFECT)
            if (native["result"] != "native-final-" + spec["responses"][attempt["number"] - 1]["kind"]
                    or finals[0]["content"] != (receipt + "\n" if claims else "") + native["result"]
                    or (claims and observations[0]["content"] != receipt)
                    or (not claims and driver["observation_id"] is not None)):
                raise ValueError("inconsistent C1 child completion content")
            result["outcome"] = "completed" if claims else "refused" if calls else "noop"
            result["references"] = [rid, driver["observation_id"], driver["final_id"]]
        elif (driver["complete"] is False and not calls and not linked and canonical(before) == canonical(after)
              and returncode == 2):
            if binding["prompt_status"] != "0" and native is None:
                result["outcome"] = "blocked"
            elif native and native["is_error"] is True and native["session_id"]:
                result["outcome"] = "retry"
        result.update(driver=driver, calls=calls)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    names = ("trigger.json", "config.json", "context.raw", "related.txt", "raw-prompt.bin", "raw-prompt.txt",
             "driver.json", "calls.jsonl", "stdout.txt", "stderr.txt", "native-run-id", "child.json", "child-closed.json",
             "canonical-after.json", "trajectory-snapshot.json")
    result["artifacts"] = [{"path": str((run / name).relative_to(root)), "size": (run / name).stat().st_size,
                            "sha256": sha((run / name).read_bytes())} for name in names if (run / name).is_file()]
    verify_artifacts(root, result["artifacts"])
    result["related"] = (run / "related.txt").read_text().splitlines()
    return result


async def wait_or_stop(delay, stop, sleep):
    waiting, stopping = asyncio.create_task(sleep(delay)), asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if waiting in done:
            await waiting
    finally:
        waiting.cancel()
        stopping.cancel()
        await asyncio.gather(waiting, stopping, return_exceptions=True)


async def run(root, spec, *, steps, stop=None, clock=time.time, sleep=asyncio.sleep, stage=None):
    stop = stop or asyncio.Event()
    root = root.resolve()
    if str(root) != spec["root"] or type(steps) is not int or not 0 <= steps <= 1000:
        raise ValueError("invalid C1 root or explicit step budget")
    with Campaign(root / "journal") as journal:
        state = State.restore(journal, spec)
        for _ in range(steps):
            if stop.is_set() or state.active or state.halted or state.attempts >= len(spec["responses"]):
                break
            state.admit(journal, clock())
            event = state.choose(clock())
            while event is None and not stop.is_set():
                due = state.wait_until(clock())
                if due is None:
                    break
                await wait_or_stop(max(0, due - clock()), stop, sleep)
                state.admit(journal, clock())
                event = state.choose(clock())
            if event is None or stop.is_set() or state.attempts >= len(spec["responses"]):
                break
            attempt = state.start(journal, event, clock(), await snapshot(root))
            if stage:
                await stage("prepared", state)
            run_root, prompt, env, binding = prepare(root, spec, state, journal, attempt)
            if stage:
                await stage("bound", state)
            if stop.is_set():
                break
            argv = [sys.executable, "/fixture/headlong_native_bridge.py", "--model", spec["model"],
                    "--traj", spec["trajectory_id"], "--workdir", str(root / "identity/workdir"), "--prompt-file", str(prompt)]
            # Any exception leaves the bound intent uncertain; no replay.
            code = await invoke(argv, env, run_root, stop)
            evidence = await collect(root, spec, attempt, run_root, binding, code)
            if stage:
                await stage("collected", state)
            state.finish(journal, evidence["outcome"], clock(), evidence)
            if stage:
                await stage("committed", state)
        status = state.halted or ("uncertain" if state.active else "stopped" if stop.is_set() else "budget")
        return {"schema": "ego-owned-loop/v1", "status": status, "state": state.summary(),
                "observations": state.observations, "source_pin": spec["source_pin"], "sources": spec["sources"]}


def source_pin():
    return Path("/fixture/genesis-source-pin").read_text().strip()


async def main(work_root=Path("/work")):
    preflight()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=1)
    args = parser.parse_args()
    root = args.root.resolve()
    if root == work_root or not root.is_relative_to(work_root.resolve()):
        raise ValueError("C1 fixture root is outside owned work")
    spec = load_json((root / "manifest.json").read_text())
    if spec["sources"] != source_hashes() or spec["source_pin"] != source_pin():
        raise ValueError("C1 source identity mismatch")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        report = await run(root, spec, steps=args.steps, stop=stop)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    print(json.dumps(report))
    return 2 if report["status"] in {"uncertain", "blocked"} else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({"schema": "ego-owned-loop/v1", "status": "incomplete", "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(2) from exc
