"""Full shared native mechanics population for the contained C1 candidate."""

import asyncio
import base64
import contextlib
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import ego_owned_controls as fixture
from ego_runtime_cases import CASES, case_for, kind_for, manifest
from ego_runtime_report import valid_c_counters
from headlong_controls import dependency_inventory, preflight, read_jsonl, tree_hash
from headlong_monolith_controls import (
    MARKERS,
    MENTION_CONTENT,
    RECEIPT,
    REFERENCE,
    captured_text,
    command,
)
from headlong_native_bridge import ReceiptView
from headlong_native_provider import Backend
from headlong_prompt_references import canonical_id

from genesis.eval.qualification.evidence import canonical


def outcomes(case):
    if case.blocked:
        return ["blocked"]
    if case.fault or case.kind == "handler_error":
        return ["uncertain"]
    if case.kind == "provider_error":
        return ["retry"]
    first = "noop" if case.kind == "echo" else "refused" if case.kind == "pending" else "completed"
    return [first] + (["refused"] if case.wakes == 2 else [])


def qualify(case, row):
    try:
        observed = row["reports"][-1]["observations"]
        expected = outcomes(case)
        effected = case.kind == "approved" and not case.blocked
        status = "executed" if effected else "approved" if case.kind == "approved" else "pending"
        wanted_after = {"state": {**row["before"]["state"], "status": "executed", "user_response": "dispatching"},
                        "effect": fixture.native.EFFECT.hex()} if effected else row["before"]
        final = row["reports"][-1]["state"]
        accepted = sum(v in {"completed", "noop", "refused"} for v in expected)
        halted = expected[-1] in {"blocked", "uncertain"}
        level = 0 if halted or case.reactive or expected[-1] == "completed" else 1
        wake = row["manifest"]["start"] if halted else observed[-1]["observed_at"] + (5 if level else 0)
        checks = [valid_c_counters(final), len(row["runs"]) == case.wakes, len(observed) == case.wakes,
                  len(row["commands"]) == len(row["reports"]) == case.wakes,
                  [o["outcome"] for o in observed] == expected, row["groups_closed"] is True,
                  canonical(row["after"]) == canonical(wanted_after), row["before"]["effect"] is None,
                  row["before"]["state"]["ego_source"] == "genesis_ego_cycle",
                  row["before"]["state"]["scope"] == "operate",
                  row["after"]["state"]["status"] == status,
                  row["after"]["effect"] == (fixture.native.EFFECT.hex() if effected else None),
                  final["cursor"] == (accepted if case.reactive else 0),
                  final["timer_done"] == (0 if case.reactive else accepted),
                  final["timer_reserved"] == (0 if case.reactive else case.wakes),
                  final["halted"] == (expected[-1] if halted else None), final["level"] == level,
                  final["wake_at"] == wake, final["active"] is None, final["attempts"] == case.wakes,
                  all(c["timed_out"] is False and c["returncode"] == (2 if halted else 0) for c in row["commands"]),
                  all(r["status"] == (expected[-1] if halted else "budget") for r in row["reports"])]
        sessions = []
        for index, run in enumerate(row["runs"]):
            driver, backend = run["driver"], row["backends"][index]
            calls, native = run["calls"], driver["native"]
            kind = kind_for(case.name, second=bool(index))
            complete = not (case.blocked or case.fault or kind in {"provider_error", "handler_error"})
            failed_handler = kind == "handler_error" or case.fault == "observation"
            errors = ["ValueError: unfinished or failed protected handler"] if failed_handler else []
            driver_error = ("ValueError: reference_conflict" if case.blocked else
                            "OSError: synthetic final append failure" if case.fault == "final" else None)
            checks.extend([driver["outer_source"] == "ego-owned-loop", driver["model"] == "sonnet",
                           driver["complete"] is complete, backend["unexpected"] == [],
                           canonical(observed[index]["evidence"]["errors"]) == canonical(errors),
                           driver["error"] == driver_error,
                           canonical(driver["calls"]) == canonical(calls),
                           run["closed"]["group_alive"] is False,
                           run["config"]["invocation_id"] == driver["invocation_id"],
                           run["config"]["trigger_id"] == driver["trigger_id"],
                           fixture.owned.sha(run["raw_prompt"].encode()) == driver["prompt_sha256"]])
            rid = canonical_id(driver["run_id"])
            header, = [r for r in row["trajectory"] if r.get("step_id") == rid]
            linked = [r for r in row["trajectory"] if r.get("run_id") == rid]
            finals = [r for r in linked if r["type"] == "final"]
            observations = [r for r in linked if r["type"] == "observation"]
            visible = effected and index == 0 and case.fault != "observation"
            checks.extend([header["source"] == "ego-owned-loop", header["engine"] == "claude-code-sdk",
                           header["model"] == "sonnet", header["trigger_step"] == driver["trigger_id"],
                           header["invocation_id"] == driver["invocation_id"],
                           header["wake"] == "owned-loop/" + ("reactive" if case.reactive else "timer"),
                           len(finals) == int(complete), len(observations) == int(visible),
                           all(r["source"] == "ego-owned-loop" for r in linked)])
            if case.blocked:
                checks.extend([native is None, backend["main_requests"] == 0, calls == [],
                               driver["prompt_status"] == "65", driver["failure_kind"] == "reference_conflict",
                               run["raw_prompt"] == "", driver["observation_id"] is driver["final_id"] is None,
                               driver["messages"] == []])
                continue
            sessions.append(native["session_id"])
            results = [m["data"] for m in driver["messages"] if m["type"] == "ResultMessage"]
            inits = [m["data"]["data"] for m in driver["messages"]
                     if m["type"] == "SystemMessage" and m["data"]["subtype"] == "init"]
            checks.extend([bool(native["session_id"]), backend["main_requests"] >= 1,
                           driver["prompt_status"] == "0", run["raw_prompt"] in captured_text(backend),
                           "@mention-target.txt" in captured_text(backend), MENTION_CONTENT not in captured_text(backend),
                           all(marker in run["raw_prompt"] for marker in MARKERS if index == 0 or marker != "fixture-memory-marker"),
                           canonical(results) == canonical([native]), len(inits) == 1,
                           inits[0]["tools"] == ["mcp__fixture__claim_effect"],
                           inits[0]["session_id"] == native["session_id"], bool(inits[0]["model"])])
            if kind == "provider_error":
                checks.extend([native["is_error"] is True, native["api_error_status"] == 400,
                               backend["provider_error"] is True, calls == [], final["retry"] is not None])
            else:
                checks.extend([native["is_error"] is False, native["subtype"] == "success",
                               native["result"] == "native-final-" + kind])
                if failed_handler:
                    error = ("OSError: synthetic observation append failure" if case.fault else
                             "RuntimeError: synthetic handler-error instrument control")
                    checks.append(canonical(calls) == canonical([{"canonical_called": True,
                                  "claim": True if case.fault else None, "outcome": "error", "error": error}]))
                else:
                    wanted = [] if kind == "echo" else [{"canonical_called": True, "claim": effected and index == 0,
                                                         "outcome": "executed" if effected and index == 0 else "refused"}]
                    checks.append(canonical(calls) == canonical(wanted))
            if visible:
                checks.extend([observations[0]["step_id"] == driver["observation_id"], observations[0]["content"] == RECEIPT])
            if complete:
                checks.extend([finals[0]["step_id"] == driver["final_id"], finals[0]["native_session_id"] == native["session_id"],
                               finals[0]["content"] == (RECEIPT + "\n" if visible else "") + native["result"]])
            if case.name == "large_context":
                checks.extend([len(run["raw_prompt"].encode()) > 131072, "λ" in run["raw_prompt"],
                               "continuity context " * 9000 in run["raw_prompt"]])
            if case.name == "reference_continuity" and index == 0:
                checks.append(rid == REFERENCE)
            if index:
                previous = row["runs"][0]["driver"]["run_id"]
                checks.extend([previous in run["raw_prompt"], RECEIPT in run["raw_prompt"],
                               row["history_lookup"]["returncode"] == 0,
                               len(row["history_rows"]) == 2,
                               all(r["run_id"] == previous for r in row["history_rows"])])
        checks.append(len(set(sessions)) == len(sessions))
        return all(checks)
    except (KeyError, ValueError, TypeError, AttributeError, IndexError):
        return False


async def scenario(root, name):
    case = case_for(name)
    spec = await fixture.seed(root, name, mechanics=case)
    before = await fixture.owned.snapshot(root)
    commands, reports = [], []
    kinds = [kind_for(name, second=bool(i)) for i in range(case.wakes)]
    with contextlib.ExitStack() as stack:
        backends = [stack.enter_context(Backend(kind, ReceiptView(root / f"attempt-{i:05d}/calls.jsonl")))
                    for i, kind in enumerate(kinds, 1)]
        spec["responses"] = [{"kind": kind, "backend_port": backend.server.server_port, "scenario": name,
                              "fault": case.fault, "fixture_header_id": REFERENCE if name == "reference_continuity" and i == 0 else None}
                             for i, (kind, backend) in enumerate(zip(kinds, backends, strict=True))]
        fixture.owned.save(root / "manifest.json", spec)
        for _ in kinds:
            result = command([sys.executable, "/fixture/ego_owned_loop.py", "--root", str(root), "--steps", "1"],
                             fixture.owned.environment(root, spec), root)
            commands.append(result)
            reports.append(json.loads(result["stdout"]))
        backend_evidence = [backend.evidence for backend in backends]
    runs = []
    for run in sorted(root.glob("attempt-*")):
        def load(name, directory=run):
            return json.loads((directory / name).read_text())
        runs.append({"driver": load("driver.json"), "config": load("config.json"),
                     "raw_prompt": (run / "raw-prompt.txt").read_text(), "calls": ReceiptView(run / "calls.jsonl").calls,
                     "closed": load("child-closed.json")})
    lookup = None
    if case.wakes == 2:
        previous = runs[0]["driver"]["run_id"]
        lookup = command(["/headlong-b/bin/traj", "tail", "-n", "400", "--filter", "run_id=" + previous],
                         fixture.owned.environment(root, spec), root / "identity/workdir")
    row = {"scenario": name, "before": before, "after": await fixture.owned.snapshot(root), "manifest": spec,
           "commands": commands, "reports": reports, "backends": backend_evidence, "runs": runs,
           "trajectory": read_jsonl(fixture.owned.trajectory(root)), "artifacts": fixture.retained(root),
           "history_lookup": lookup, "history_rows": [] if lookup is None else [json.loads(v) for v in lookup["stdout"].splitlines()],
           "groups_closed": all(not fixture.owned.group_alive(r["closed"]["pgid"]) for r in runs)}
    row["control_passed"] = qualify(case, row)
    return row


def population():
    return {"schema": "ego-owned-native-population/v1", "comparison_manifest": manifest(),
            "cases": [{"name": c.name, "outcomes": outcomes(c), "attempts": c.wakes,
                       "native_sessions": 0 if c.blocked else c.wakes} for c in CASES]}


async def main():
    preflight()
    import claude_agent_sdk
    from ego_owned_state_controls import complete_population, sweep
    from ego_owned_state_controls import population as state_population

    cli = Path(claude_agent_sdk.__file__).parent / "_bundled/claude"
    roots = {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}
    sources = {k: tree_hash(Path(v)) for k, v in roots.items()}
    declared = {"native": population(), "state": state_population()}
    with TemporaryDirectory(dir="/work") as directory:
        root = Path(directory)
        fixture.owned.save(root / "population.json", declared)
        cases = []
        for case in CASES:
            owned_root = root / case.name
            try:
                cases.append(await scenario(owned_root, case.name))
            except Exception as exc:
                cases.append({"scenario": case.name, "control_passed": False, "error": f"{type(exc).__name__}: {exc}",
                              "artifacts": fixture.retained(owned_root) if (owned_root / "manifest.json").exists() else []})
        state_root = root / "state"
        try:
            state = sweep(state_root, fixture.owned.source_pin(), fixture.owned.source_hashes())
        except Exception as exc:
            state = {"status": "incomplete", "error": f"{type(exc).__name__}: {exc}"}
        state["journals"] = [{"path": str(p.relative_to(state_root)), "sha256": fixture.owned.sha(p.read_bytes()),
                              "size": p.stat().st_size, "content_b64": base64.b64encode(p.read_bytes()).decode()}
                             for p in sorted(state_root.rglob("answers.jsonl"))]
    unchanged = sources == {k: tree_hash(Path(v)) for k, v in roots.items()}
    sessions = [run["driver"]["native"]["session_id"] for case in cases for run in case.get("runs", [])
                if run["driver"]["native"] is not None]
    expected_sessions = sum(c["native_sessions"] for c in declared["native"]["cases"])
    passed = (unchanged and all(c["control_passed"] is True for c in cases) and state["status"] == "pass"
              and complete_population(declared["state"], state["rows"])
              and len(sessions) == len(set(sessions)) == expected_sessions)
    print(json.dumps({"schema": "ego-owned-matrix/v1", "status": "pass" if passed else "incomplete",
                      "declared_population": declared, "population_sha256": fixture.owned.sha(canonical(declared)),
                      "cases": cases, "state_controls": state, "native_sessions": len(sessions),
                      "sources": sources, "sources_unchanged": unchanged,
                      "cleanup_confirmed": all(c.get("groups_closed") is True for c in cases),
                      "source_pin": fixture.owned.source_pin(), "sdk": importlib.metadata.version("claude-agent-sdk"),
                      "native_binary": {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
                                        "version": subprocess.check_output([str(cli), "--version"], text=True, timeout=10).strip()},
                      "dependencies": dependency_inventory(),
                      "scope": "shared native mechanics plus separate pure reducer population; crash/full workflow/Codex remain unqualified"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
