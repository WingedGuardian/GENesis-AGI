"""Actual-clock/lifecycle qualification; native execution only in contained CI."""

import asyncio
import base64
import contextlib
import copy
import hashlib
import importlib.metadata
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import ego_owned_controls as fixture
import ego_owned_processes as processes
import ego_owned_recovery as recovery
from ego_runtime_report import finite_number, valid_c_counters
from headlong_controls import dependency_inventory, preflight, read_jsonl, tree_hash
from headlong_native_bridge import ReceiptView
from headlong_native_provider import Backend


def population():
    return {"schema": "ego-owned-timing-population/v1", "timer_wakes": 6, "timer_waits": [5, 10, 20, 40, 40],
            "reactive_offsets": [3, 5, 8], "lifecycle": ["sigterm-backoff", "exclusive-writer"],
            "scope": "actual clock and CLI lifecycle; pure reducer and recovery populations are separate"}


def argv(root, steps, probe=False):
    return [sys.executable, "/fixture/ego_owned_probe.py" if probe else "/fixture/ego_owned_loop.py",
            "--root", str(root), "--steps", str(steps)]


@contextlib.contextmanager
def launched(root, spec, steps, probe=False):
    process = subprocess.Popen(argv(root, steps, probe), env=fixture.owned.environment(root, spec), cwd=root,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            runs = sorted(root.glob("attempt-*/child.json"))
            child = json.loads(runs[-1].read_text()) if runs else None
            try:
                groups = processes.scopes(process, processes.census(), child)
            except processes.OwnershipError:
                process.kill()
                process.wait(timeout=60)
                process.stdout.close()
                process.stderr.close()
                raise
            processes.stop(process, groups)
        process.wait(timeout=60)
        process.stdout.close()
        process.stderr.close()


def finished(process):
    stdout, stderr = process.communicate(timeout=240)
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}


def completed_journal(root):
    # Observer only: retry an in-progress append. The actual C reader remains strict.
    try:
        return read_jsonl(root / "journal/answers.jsonl")
    except (FileNotFoundError, ValueError):
        return []


def prepared(rows):
    return [r["data"]["attempt"] for r in rows if r["kind"] == "dispatch" and r["data"]["stage"] == "prepared"]


def observations(rows):
    return [r["data"] for r in rows if r["kind"] == "observation"]


def classify(offset, spec, rows):
    first = observations(rows)[0]
    due = spec["start"] + offset
    return "already-pending" if due <= first["observed_at"] else "during-backoff" if due < first["next"]["wake_at"] else "later"


def captured_journal(artifacts):
    item, = [a for a in artifacts if a["path"] == "journal/answers.jsonl"]
    data = base64.b64decode(item["content_b64"], validate=True)
    if type(item["size"]) is not int or item["size"] != len(data) or fixture.owned.sha(data) != item["sha256"] or not data.endswith(b"\n"):
        raise ValueError("invalid captured journal bytes")
    return data, [json.loads(line) for line in data.split(b"\n")[:-1]]


def first_writer(row):
    writer = row["writer"]
    before, prefix = captured_journal(writer["artifacts"])
    after, final = captured_journal(row["artifacts"])
    first = row["reports"][0]
    return (prefix == writer["journal"] == final[:4] and final == row["journal"] and after.startswith(before)
            and [r["kind"] for r in prefix] == ["manifest", "dispatch", "dispatch", "observation"]
            and prefix[0]["data"] == row["manifest"]
            and prefix[1]["data"]["stage"] == "prepared" and prefix[2]["data"]["stage"] == "bound"
            and first["observations"] == [prefix[3]["data"]]
            and prefix[3]["data"] == observations(final)[0] and first["state"] == prefix[3]["data"]["next"])


def qualify(name, row):
    try:
        report, spec, rows = row["reports"][-1], row["manifest"], row["journal"]
        attempts, observed = prepared(rows), observations(rows)
        state = report["state"]
        count = 6 if name == "timers" else 3 if name.startswith("reactive-") else 1
        responses = 2 if name == "sigterm-backoff" else count
        if (not valid_c_counters(state) or state["attempts"] != count or len(attempts) != count or len(observed) != count
                or report["observations"] != observed or row["scopes_closed"] is not True
                or len(spec["responses"]) != responses or len(row["backends"]) != responses
                or len(row["commands"]) != (2 if name == "timers" else 1)
                or any(c["returncode"] != 0 or c["timed_out"] is not False for c in row["commands"])
                or any(b["unexpected"] for b in row["backends"]) or state["active"] is not None):
            return False
        completed = name == "exclusive-writer"
        expected = "completed" if completed else "noop"
        if any(o["outcome"] != expected or o["evidence"]["errors"] for o in observed):
            return False
        if sum(b["main_requests"] for b in row["backends"]) != (2 if completed else count):
            return False
        if row["after"]["effect"] != (fixture.native.EFFECT.hex() if completed else None):
            return False
        for i, (attempt, observation) in enumerate(zip(attempts, observed, strict=True)):
            driver = observation["evidence"]["driver"]
            if (driver["complete"] is not True or driver["native"]["is_error"] is not False
                    or driver["native"]["result"] != ("native-final-approved" if completed else "native-final-echo")
                    or row["backends"][i]["main_requests"] != (2 if completed else 1)):
                return False
            if not finite_number(attempt["started_at"]) or not finite_number(observation["observed_at"]):
                return False
            if observation["observed_at"] < attempt["started_at"]:
                return False
            if i and attempt["event"]["kind"] == "timer" and attempt["started_at"] < observed[i-1]["next"]["wake_at"] - .05:
                return False
        if name == "timers":
            first = row["reports"][0]
            return (report["status"] == "budget" and state["cursor"] == 0 and state["timer_done"] == 6 and state["level"] == 6
                    and first["state"] == observed[0]["next"] and len(first["observations"]) == 1
                    and first_writer(row)
                    and all(a["event"]["kind"] == "timer" for a in attempts)
                    and all(o["next"]["wake_at"] == o["observed_at"] + delay
                            for o, delay in zip(observed, [5, 10, 20, 40, 40, 40], strict=True)))
        if name.startswith("reactive-"):
            event, = spec["feed"]
            offset = row["offset"]
            index, = [i for i, a in enumerate(attempts) if a["event"]["kind"] == "reactive"]
            actual = classify(offset, spec, rows)
            return (offset in population()["reactive_offsets"] and event["due"] == spec["start"] + offset
                    and row["classification"] == actual and report["status"] == "budget"
                    and state["cursor"] == 1 and state["timer_done"] == 2
                    and attempts[index]["event"]["id"] == event["id"] and attempts[index]["started_at"] >= event["due"] - .05
                    and observed[index]["next"]["level"] == 0
                    and (actual != "during-backoff" or index == 1 and attempts[index]["started_at"] < observed[0]["next"]["wake_at"]))
        if name == "sigterm-backoff":
            return (report["status"] == "stopped" and len(spec["responses"]) == 2
                    and state == observed[0]["next"] and state["cursor"] == 0 and state["timer_done"] == 1
                    and observed[0]["observed_at"] <= row["signal_at"] < state["wake_at"])
        if name == "exclusive-writer":
            second = row["contender"]
            return (report["status"] == "budget" and state["cursor"] == 1 and state["timer_done"] == 0
                    and second["command"]["returncode"] == 2 and second["command"]["timed_out"] is False
                    and second["report"] == {"schema": "ego-owned-loop/v1", "status": "incomplete",
                                             "error": "Incomplete: campaign is in use by another session"}
                    and second["requests_before"] == second["requests_after"] == 0
                    and second["journal_before"] == second["journal_after"])
        return False
    except (KeyError, ValueError, TypeError, IndexError):
        return False


def calibration(row):
    controls = []
    for field, value in (("journal", []), ("scopes_closed", False), ("backends", []), ("reports", [])):
        mutant = copy.deepcopy(row)
        mutant[field] = value
        controls.append({"case": row["scenario"], "corruption": field, "rejected": qualify(row["scenario"], mutant) is False})
    return controls


async def scenario(root, name, offset=None):
    baseline = set(processes.census())
    count = 6 if name == "timers" else 3 if offset is not None else 2 if name == "sigterm-backoff" else 1
    kind = "approved" if name == "exclusive-writer" else "echo"
    spec = await fixture.seed(root, "restart_effect" if kind == "approved" else "timers")
    if offset is not None or kind == "approved":
        spec["feed"] = [{"id": "external-1", "seq": 1, "due": spec["start"] + (offset or 0), "source": "fixture",
                         "target": "genesis_ego_cycle", "type": "observation", "content": "continuity orbit wake"}]
    commands, reports, details = [], [], {}
    with contextlib.ExitStack() as stack:
        backends = [stack.enter_context(Backend(kind, ReceiptView(root / f"attempt-{i:05d}/calls.jsonl"))) for i in range(1, count+1)]
        spec["responses"] = [{"kind": kind, "backend_port": b.server.server_port, "scenario": name,
                              "fault": None, "fixture_header_id": None} for b in backends]
        fixture.owned.save(root / "manifest.json", spec)
        if kind == "approved":
            fixture.owned.save(root / "control.json", {"root": str(root), "sources": spec["sources"],
                "source_pin": spec["source_pin"], "mode": "crash", "stage": "bound"})
        for steps in ([1, 5] if name == "timers" else [count]):
            with launched(root, spec, steps, probe=kind == "approved") as process:
                if name == "sigterm-backoff":
                    processes.wait_for(lambda: len(observations(completed_journal(root))) == 1, process)
                    details["signal_at"] = time.time()
                    process.send_signal(signal.SIGTERM)
                if kind == "approved":
                    processes.wait_for((root / "probe-bound.json").exists, process)
                    before = (root / "journal/answers.jsonl").read_text()
                    requests = sum(b.evidence["main_requests"] for b in backends)
                    try:
                        with launched(root, spec, 1) as contender:
                            result = finished(contender)
                        details["contender"] = {"command": result, "report": json.loads(result["stdout"]),
                            "requests_before": requests, "requests_after": sum(b.evidence["main_requests"] for b in backends),
                            "journal_before": before, "journal_after": (root / "journal/answers.jsonl").read_text()}
                    finally:
                        (root / "release").write_text("release owned bound barrier")
                result = finished(process)
            commands.append(result)
            reports.append(json.loads(result["stdout"]))
            if name == "timers" and len(reports) == 1:
                details["writer"] = recovery.boundary(root)
        evidence = [b.evidence for b in backends]
    rows = completed_journal(root)
    if offset is not None:
        details.update(offset=offset, classification=classify(offset, spec, rows))
    row = {"scenario": name, "manifest": spec, "commands": commands, "reports": reports, "journal": rows,
           "after": await fixture.owned.snapshot(root), "backends": evidence,
           "baseline_pids": sorted(baseline), "census_after": recovery.assert_no_survivors(baseline),
           "scopes_closed": True, "artifacts": recovery.retained(root), **details}
    row["passed"] = qualify(name, row)
    return row


async def main():
    preflight()
    import claude_agent_sdk

    init = processes.init_identity("/fixture/ego_owned_timing.py")
    roots = {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}
    sources = {k: tree_hash(Path(v)) for k, v in roots.items()}
    declared, rows, controls, challenge = population(), [], [], None
    with TemporaryDirectory(dir="/work") as directory:
        root = Path(directory)
        fixture.owned.save(root / "population.json", declared)
        cells = [("timers", None)] + [(f"reactive-{n}", n) for n in declared["reactive_offsets"]]
        cells += [(name, None) for name in declared["lifecycle"]]
        try:
            for name, offset in cells:
                rows.append(await scenario(root / name, name, offset))
                if rows[-1]["passed"] is not True:
                    raise RuntimeError("actual timing/lifecycle predicate failed: " + name)
                controls.extend(calibration(rows[-1]))
                if not all(c["rejected"] for c in controls):
                    raise RuntimeError("timing instrument accepted missing evidence")
            if not any(row.get("classification") == "during-backoff" for row in rows):
                raise RuntimeError("no fixed arrival landed during actual backoff")
        except Exception as exc:
            challenge = {"error": f"{type(exc).__name__}: {exc}", "material_scope_challenge": isinstance(exc, processes.OwnershipError),
                         "artifacts": {p.name: recovery.retained(p) for p in root.iterdir() if p.is_dir()}}
    unchanged = sources == {k: tree_hash(Path(v)) for k, v in roots.items()}
    passed = challenge is None and unchanged and len(rows) == 6
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled/claude"
    print(json.dumps({"schema": "ego-owned-timing/v1", "status": "pass" if passed else "incomplete", "source_pin": fixture.owned.source_pin(),
        "declared_population": declared, "cases": rows, "calibration": controls, "challenge": challenge, "init": init, "sources": sources, "sources_unchanged": unchanged,
        "sdk": importlib.metadata.version("claude-agent-sdk"), "dependencies": dependency_inventory(),
        "native_binary": {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
                          "version": subprocess.check_output([str(cli), "--version"], text=True, timeout=10).strip()},
        "scope": "offline actual clock/lifecycle only; full workflow, useful model behavior and Codex remain unqualified"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
