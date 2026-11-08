"""Contained real-process C1 interruption/late-I/O qualification controller."""

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import ego_owned_controls as fixture
import ego_owned_probe as probe
import ego_owned_processes as processes
from ego_runtime_report import valid_c_counters
from headlong_controls import dependency_inventory, preflight, read_jsonl, tree_hash
from headlong_monolith_controls import RECEIPT, command
from headlong_native_bridge import ReceiptView
from headlong_native_provider import Backend

from genesis.eval.qualification.evidence import canonical


class HeldBackend(Backend):
    def __init__(self, kind, port, root, hold):
        super().__init__(kind, port)
        self.root, self.hold, self.release = root, hold, threading.Event()

    def respond(self, path, payload):
        result = super().respond(path, payload)
        if self.hold and self.evidence["main_requests"] == 1:
            config = json.loads((self.root / "attempt-00001/config.json").read_text())
            probe.mark(self.root, "native", nonce=config["invocation_id"], main_requests=1, owner="backend")
            self.release.wait()
        return result

    def __exit__(self, *args):
        self.release.set()
        return super().__exit__(*args)


def population():
    return {"schema": "ego-owned-recovery-population/v1", "crashes": list(probe.CRASHES),
            "late_io": list(probe.FAULTS), "calibration": ["init-adoption", "ordinary-effect", "no-op-rejected"],
            "scope": "instrumented real-process recovery; native mechanics/state/timing remain separate populations"}


def snapshot_processes():
    return {str(pid): row for pid, row in processes.census().items()}


def assert_no_survivors(baseline):
    rows = processes.census()
    survivors = set(rows) - baseline
    if survivors:
        raise processes.OwnershipError("unaccounted process survives in owned container: " + str(sorted(survivors)))
    return {str(k): v for k, v in rows.items()}


def reopen(root, spec):
    result = command([sys.executable, "/fixture/ego_owned_loop.py", "--root", str(root), "--steps", "1"],
                     fixture.owned.environment(root, spec), root)
    return result, json.loads(result["stdout"])


async def seed(root, stage, mode, backend, kind="approved"):
    spec = await fixture.seed(root, "restart_effect")
    spec["feed"] = [{"id": "external-1", "seq": 1, "due": spec["start"], "source": "fixture",
                     "target": "genesis_ego_cycle", "type": "observation", "content": "continuity orbit wake"}]
    response = {"kind": kind, "backend_port": backend.server.server_port,
                "scenario": "recovery", "fault": None, "fixture_header_id": None}
    spare = mode == "late" or (mode == "crash" and stage not in {"before_intake", "after_intake", "committed"})
    spec["responses"] = [dict(response) for _ in range(2 if spare else 1)]
    fixture.owned.save(root / "manifest.json", spec)
    fixture.owned.save(root / "control.json", {"root": str(root), "sources": spec["sources"], "source_pin": spec["source_pin"],
                       "mode": mode, "stage": stage})
    return spec


def journal(root):
    return read_jsonl(root / "journal/answers.jsonl")


def retained(root):
    items = fixture.retained(root) if (root / "manifest.json").exists() else []
    for path in sorted(root.glob("probe-*.json")) + ([root / "control.json"] if (root / "control.json").exists() else []):
        data = path.read_bytes()
        items.append({"path": path.name, "size": len(data), "sha256": fixture.owned.sha(data),
                      "content_b64": base64.b64encode(data).decode()})
    return items


def boundary(root):
    run = root / "attempt-00001"
    def optional(name):
        path = run / name
        return json.loads(path.read_text()) if path.exists() else None
    return {"journal": journal(root), "trajectory": read_jsonl(fixture.owned.trajectory(root)),
            "config": optional("config.json"), "driver": optional("driver.json"), "artifacts": retained(root)}


def qualify_writer(stage, writer, spec):
    try:
        early = {"before_intake", "after_intake"}
        bound = stage in {"bound", "native", "claimed", "effect", "receipt", "collected", "committed"}
        terminal = stage in {"receipt", "collected", "committed"}
        kinds = (["manifest"] + (["acknowledgement"] if stage != "before_intake" else [])
                 + (["dispatch"] if stage not in early else []) + (["dispatch"] if bound else [])
                 + (["observation"] if stage == "committed" else []))
        rows = writer["journal"]
        if [r["kind"] for r in rows] != kinds or canonical(rows[0]["data"]) != canonical(spec):
            return False
        if stage != "before_intake" and canonical(rows[1]["data"]["event"]) != canonical(spec["feed"][0]):
            return False
        if stage not in early and rows[2]["data"]["stage"] != "prepared":
            return False
        if bound and rows[3]["data"]["stage"] != "bound":
            return False
        triggers = [r for r in writer["trajectory"] if r.get("input_event_id") == spec["feed"][0]["id"]]
        headers = [r for r in writer["trajectory"] if r["type"] == "shellm-run"]
        if len(triggers) != int(stage not in early | {"prepared"}) or len(headers) != int(bound and stage != "bound"):
            return False
        config, driver = writer["config"], writer["driver"]
        if (config is not None) != (bound or stage == "config") or (driver is not None) != terminal:
            return False
        if config is not None and config["trigger_id"] != triggers[0]["step_id"]:
            return False
        if bound and rows[3]["data"]["binding"]["nonce"] != config["invocation_id"]:
            return False
        linked = [r for r in writer["trajectory"] if headers and r.get("run_id") == headers[0]["step_id"]]
        if [r["type"] for r in linked] != (["observation", "final"] if terminal else []):
            return False
        if terminal and (driver["complete"] is not True or driver["run_id"] != headers[0]["step_id"]
                         or driver["invocation_id"] != config["invocation_id"]):
            return False
        return stage != "committed" or rows[-1]["data"]["outcome"] == "completed"
    except (KeyError, ValueError, TypeError, IndexError):
        return False


def qualify_crash(stage, row):
    try:
        resumable = stage in {"before_intake", "after_intake"}
        committed = stage == "committed"
        final = row["reopened"]["state"]
        before_effect = stage in {"effect", "receipt", "collected", "committed"}
        claimed = before_effect or stage == "claimed"
        expected_after_kill = {"state": {**row["before"]["state"], "status": "executed", "user_response": "dispatching"},
                               "effect": fixture.native.EFFECT.hex() if before_effect else None} if claimed else row["before"]
        final_effect = before_effect or resumable
        expected_after = {"state": {**row["before"]["state"], "status": "executed", "user_response": "dispatching"},
                          "effect": fixture.native.EFFECT.hex()} if final_effect else expected_after_kill
        observations = [r["data"] for r in row["journal"] if r["kind"] == "observation"]
        expected_calls = (0 if stage in {"before_intake", "after_intake", "prepared", "trigger", "config", "bound"}
                          else 1 if stage in {"native", "claimed", "effect"} else 2)
        writer = row["writer"]
        return all([valid_c_counters(final), len(row["manifest"]["responses"]) == (1 if resumable or committed else 2),
                    qualify_writer(stage, writer, row["manifest"]),
                    canonical(row["journal"][:len(writer["journal"])]) == canonical(writer["journal"]),
                    row["stage"] == stage, row["marker"]["stage"] == stage,
                    row["marker"]["root"] == row["manifest"]["root"],
                    row["killed"]["returncode"] == -9, row["killed"]["timed_out"] is False,
                    row["reopen_command"]["timed_out"] is False,
                    row["reopen_command"]["returncode"] == (0 if resumable or committed else 2),
                    row["reopened"]["status"] == ("budget" if resumable or committed else "uncertain"),
                    canonical(row["after_kill"]) == canonical(expected_after_kill),
                    canonical(row["after"]) == canonical(expected_after), row["scopes_closed"] is True,
                    final["cursor"] == int(resumable or committed), final["timer_done"] == final["timer_reserved"] == 0,
                    final["attempts"] == 1, len(observations) == int(resumable or committed),
                    all(o["outcome"] == "completed" and o["evidence"]["errors"] == [] for o in observations),
                    row["requests_before_reopen"] == expected_calls,
                    type(row["requests_before_reopen"]) is type(row["requests_after_reopen"]) is int,
                    row["requests_after_reopen"] == expected_calls + 2 * int(resumable),
                    row["backend"]["unexpected"] == [],
                    row["after"]["effect"] == (fixture.native.EFFECT.hex() if final_effect else None)])
    except (KeyError, ValueError, TypeError, IndexError):
        return False


async def crash(root, stage):
    baseline = set(processes.census())
    with HeldBackend("approved", ReceiptView(root / "attempt-00001/calls.jsonl"), root, stage == "native") as backend:
        spec = await seed(root, stage, "crash", backend)
        before = await fixture.owned.snapshot(root)
        env = fixture.owned.environment(root, spec)
        outer = subprocess.Popen([sys.executable, "/fixture/ego_owned_probe.py", "--root", str(root), "--steps", "1"],
                                 env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            marker_path = root / ("probe-" + stage + ".json")
            processes.wait_for(marker_path.exists, outer)
            marker = json.loads(marker_path.read_text())
            census = processes.census()
            child_path = root / "attempt-00001/child.json"
            child = json.loads(child_path.read_text()) if child_path.exists() else None
            fixture.owned.save(root / "probe-processes.json", {"outer": outer.pid, "child": child,
                               "census": {str(k): v for k, v in census.items()}})
            groups = processes.scopes(outer, census, child)
            killed = processes.stop(outer, groups)
        finally:
            backend.release.set()
            if outer.poll() is None:
                # Direct Popen ownership is certain even if descendant attribution
                # failed. No unproven group is signalled; container teardown follows.
                outer.kill()
                outer.wait(timeout=60)
                outer.stdout.close()
                outer.stderr.close()
        after_processes = assert_no_survivors(baseline)
        after_kill = await fixture.owned.snapshot(root)
        writer = boundary(root)
        requests = backend.evidence["main_requests"]
        reopened_command, report = reopen(root, spec)
        reopened_processes = assert_no_survivors(baseline)
        row = {"stage": stage, "marker": marker, "before": before, "after_kill": after_kill,
               "after": await fixture.owned.snapshot(root), "manifest": spec, "killed": killed,
               "census_at_barrier": {str(k): v for k, v in census.items()}, "groups": sorted(groups), "scopes_closed": True,
               "baseline_pids": sorted(baseline), "census_after_kill": after_processes, "census_after_reopen": reopened_processes,
               "reopen_command": reopened_command, "reopened": report, "journal": journal(root),
               "writer": writer,
               "requests_before_reopen": requests, "requests_after_reopen": backend.evidence["main_requests"],
               "backend": backend.evidence, "artifacts": retained(root)}
        row["passed"] = qualify_crash(stage, row)
        return row


def qualify_late(phase, row):
    try:
        report = row["reports"][0]
        native = row["native"]
        header, = [r for r in row["trajectory"] if r["type"] == "shellm-run"]
        linked = [r for r in row["trajectory"] if r.get("run_id") == header["step_id"]]
        observation, = [r for r in linked if r["type"] == "observation"]
        final, = [r for r in linked if r["type"] == "final"]
        expected = {"state": {**row["before"]["state"], "status": "executed", "user_response": "dispatching"},
                    "effect": fixture.native.EFFECT.hex()}
        results = [m["data"] for m in native["messages"] if m["type"] == "ResultMessage"]
        init, = [m["data"]["data"] for m in native["messages"] if m["type"] == "SystemMessage" and m["data"]["subtype"] == "init"]
        evidence = report["observations"][0]["evidence"]
        missing = FileNotFoundError(2, "No such file or directory", str(Path(row["manifest"]["root"]) / "attempt-00001/driver.json"))
        child = row["child"]
        pending = row["pending"]
        pending_valid = (row["pending_size"] == (3 if phase == "driver_short" else 0)
                         if phase not in {"driver_close", "rename"} else
                         pending["complete"] is True and pending["run_id"] == header["step_id"]
                         and pending["invocation_id"] == native["nonce"]
                         and canonical(pending["native"]) == canonical(native["native"])
                         and canonical(pending["calls"]) == canonical(row["calls"]))
        return all([row["phase"] == row["marker"]["fault"] == phase,
                    len(row["manifest"]["responses"]) == 2,
                    [r["kind"] for r in row["writer"]["journal"]] == ["manifest", "acknowledgement", "dispatch", "dispatch", "observation"],
                    canonical(row["writer"]["journal"]) == canonical(row["journal"]),
                    canonical(row["writer"]["journal"][-1]["data"]) == canonical(report["observations"][0]),
                    row["marker"]["root"] == row["manifest"]["root"],
                    row["marker"]["nonce"] == native["nonce"] == header["invocation_id"],
                    row["scopes_closed"] is True, row["published"] is False,
                    canonical(evidence["errors"]) == canonical(["FileNotFoundError: " + str(missing)]),
                    canonical(evidence["after"]) == canonical(expected),
                    type(child["pid"]) is int and child["pid"] > 1 and child["pid"] == child["pgid"],
                    canonical(row["closed"]) == canonical({**child, "returncode": 2, "group_alive": False}),
                    canonical(row["after"]) == canonical(expected),
                    all(valid_c_counters(r["state"]) and r["status"] == "uncertain" and r["state"]["cursor"] == r["state"]["timer_done"] == 0
                        and r["state"]["attempts"] == 1 for r in row["reports"]),
                    canonical(row["reports"][1]["state"]) == canonical(report["state"]),
                    len(report["observations"]) == 1 and report["observations"][0]["outcome"] == "uncertain",
                    all(c["returncode"] == 2 and c["timed_out"] is False for c in row["commands"]),
                    row["backend"]["main_requests"] >= 1, row["backend"]["unexpected"] == [],
                    row["requests_before_reopen"] == row["backend"]["main_requests"],
                    native["native"]["is_error"] is False, native["native"]["subtype"] == "success",
                    native["native"]["result"] == "native-final-approved", bool(native["native"]["session_id"]),
                    canonical(results) == canonical([native["native"]]), init["tools"] == ["mcp__fixture__claim_effect"],
                    init["session_id"] == native["native"]["session_id"], bool(init["model"]),
                    canonical(row["calls"]) == canonical([{"canonical_called": True, "claim": True, "outcome": "executed"}]),
                    observation["content"] == RECEIPT, final["content"] == RECEIPT + "\nnative-final-approved",
                    final["native_session_id"] == native["native"]["session_id"],
                    pending_valid])
    except (KeyError, ValueError, TypeError, IndexError):
        return False


async def late(root, phase, kind="approved"):
    baseline = set(processes.census())
    with Backend(kind, ReceiptView(root / "attempt-00001/calls.jsonl")) as backend:
        spec = await seed(root, phase, "pass" if phase is None else "late", backend, kind)
        before = await fixture.owned.snapshot(root)
        result = command([sys.executable, "/fixture/ego_owned_probe.py", "--root", str(root), "--steps", "1"],
                         fixture.owned.environment(root, spec), root)
        report = json.loads(result["stdout"])
        writer = boundary(root)
        requests = backend.evidence["main_requests"]
        second, restored = reopen(root, spec)
        after_processes = assert_no_survivors(baseline)
        run = root / "attempt-00001"
        pending = run / "driver.pending"
        data = pending.read_bytes() if pending.exists() else b""
        try:
            pending_data = json.loads(data)
        except ValueError:
            pending_data = None
        row = {"phase": phase, "before": before, "after": await fixture.owned.snapshot(root), "manifest": spec,
               "commands": [result, second], "reports": [report, restored], "requests_before_reopen": requests,
               "backend": backend.evidence, "marker": json.loads((root / "probe-late.json").read_text()) if phase else None,
               "native": json.loads((run / "probe-native.json").read_text()), "calls": ReceiptView(run / "calls.jsonl").calls,
               "published": (run / "driver.json").exists(), "pending_size": len(data), "pending": pending_data,
               "child": json.loads((run / "child.json").read_text()), "closed": json.loads((run / "child-closed.json").read_text()),
               "baseline_pids": sorted(baseline), "census_after_reopen": after_processes,
               "trajectory": read_jsonl(fixture.owned.trajectory(root)), "scopes_closed": True, "artifacts": retained(root)}
        row.update(writer=writer, journal=journal(root))
        row["passed"] = qualify_late(phase, row) if phase else qualify_ordinary(row)
        return row


def qualify_ordinary(row):
    try:
        expected = {"state": {**row["before"]["state"], "status": "executed", "user_response": "dispatching"},
                    "effect": fixture.native.EFFECT.hex()}
        return all([row["published"] is True, row["scopes_closed"] is True,
                    canonical(row["after"]) == canonical(expected),
                    all(valid_c_counters(r["state"]) and r["status"] == "budget" and r["state"]["cursor"] == 1 for r in row["reports"]),
                    canonical(row["reports"][0]["state"]) == canonical(row["reports"][1]["state"]),
                    all(c["returncode"] == 0 and c["timed_out"] is False for c in row["commands"]),
                    row["requests_before_reopen"] == row["backend"]["main_requests"] == 2,
                    row["backend"]["unexpected"] == [], row["native"]["native"]["is_error"] is False,
                    row["native"]["native"]["result"] == "native-final-approved"])
    except (KeyError, TypeError, ValueError):
        return False


def qualify_adoption(row):
    try:
        outer, child = row["outer"], row["child"]
        return all([row["before"][str(child)]["ppid"] == outer, row["adopted"][str(child)]["ppid"] == 1,
                    row["adopted"][str(child)]["pgid"] == child,
                    all(v["pgid"] not in {outer, child} for v in row["after"].values()),
                    str(child) not in row["after"], str(outer) not in row["after"], row["closed"] is True])
    except (KeyError, TypeError):
        return False


def adoption(root):
    root.mkdir()
    baseline = set(processes.census())
    marker = root / "probe-adoption.json"
    nonce = os.urandom(16).hex()
    code = ("import json,os,signal,subprocess,sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'],start_new_session=True); "
            "f=Path(sys.argv[1]); t=f.with_suffix('.pending'); "
            "t.write_text(json.dumps({'outer':os.getpid(),'child':p.pid,'nonce':sys.argv[2]})); "
            "t.replace(f); signal.pause()")
    outer = subprocess.Popen([sys.executable, "-c", code, str(marker), nonce], start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    groups, closed = None, False
    try:
        processes.wait_for(marker.exists, outer)
        record = json.loads(marker.read_text())
        if record["outer"] != outer.pid or record["nonce"] != nonce:
            raise processes.OwnershipError("misbound adoption calibration")
        child = record["child"]
        before = processes.census()
        groups = processes.scopes(outer, before, {"pid": child, "pgid": child})
        os.killpg(outer.pid, signal.SIGKILL)
        outer.wait(timeout=60)
        processes.wait_for(lambda: processes.census().get(child, {}).get("ppid") == 1)
        adopted = snapshot_processes()
        processes.stop(outer, groups)
        closed = True
        assert_no_survivors(baseline)
        row = {"outer": outer.pid, "child": child, "before": {str(k): v for k, v in before.items()},
               "adopted": adopted, "after": snapshot_processes(), "closed": True}
        mutant = {**row, "after": adopted}
        row["passed"] = qualify_adoption(row) and not qualify_adoption(mutant)
        row["missing_closure_rejected"] = not qualify_adoption(mutant)
        row["artifacts"] = retained(root)
        return row
    finally:
        if groups is not None and not closed:
            processes.stop(outer, groups)
        elif outer.poll() is None:
            outer.kill()
            outer.wait(timeout=60)
            outer.stdout.close()
            outer.stderr.close()


async def main():
    preflight()
    import claude_agent_sdk

    init = processes.init_identity()
    roots = {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}
    sources = {k: tree_hash(Path(v)) for k, v in roots.items()}
    declared = population()
    rows, faults, calibration, challenge = [], [], {}, None
    with TemporaryDirectory(dir="/work") as directory:
        root = Path(directory)
        fixture.owned.save(root / "population.json", declared)
        try:
            calibration["init-adoption"] = adoption(root / "adoption")
            if calibration["init-adoption"]["passed"] is not True:
                raise processes.OwnershipError("container init adoption oracle failed")
            calibration["ordinary-effect"] = await late(root / "ordinary", None)
            calibration["no-op-rejected"] = await late(root / "noop", None, "echo")
            if not calibration["ordinary-effect"]["passed"] or calibration["no-op-rejected"]["passed"] is not False:
                raise RuntimeError("recovery ordinary/no-op instrument failed")
            for stage in declared["crashes"]:
                rows.append(await crash(root / stage, stage))
                if rows[-1]["passed"] is not True:
                    raise RuntimeError("crash predicate failed: " + stage)
            for phase in declared["late_io"]:
                faults.append(await late(root / phase, phase))
                if faults[-1]["passed"] is not True:
                    raise RuntimeError("late-I/O predicate failed: " + phase)
        except Exception as exc:
            challenge = {"error": f"{type(exc).__name__}: {exc}", "material_scope_challenge": isinstance(exc, processes.OwnershipError),
                         "artifacts": {p.name: retained(p) for p in root.iterdir() if p.is_dir()}}
    unchanged = sources == {k: tree_hash(Path(v)) for k, v in roots.items()}
    passed = challenge is None and unchanged and len(rows) == 12 and len(faults) == 7
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled/claude"
    print(json.dumps({"schema": "ego-owned-recovery/v1", "status": "pass" if passed else "incomplete",
                      "declared_population": declared, "population_sha256": fixture.owned.sha(canonical(declared)),
                      "init": init, "calibration": calibration, "crashes": rows, "late_io": faults, "challenge": challenge,
                      "sources": sources, "sources_unchanged": unchanged, "source_pin": fixture.owned.source_pin(),
                      "sdk": importlib.metadata.version("claude-agent-sdk"), "dependencies": dependency_inventory(),
                      "native_binary": {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
                                        "version": subprocess.check_output([str(cli), "--version"], text=True, timeout=10).strip()},
                      "scope": "offline marker-instrumented process recovery and late-I/O only; timing/full workflow/Codex unqualified"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
