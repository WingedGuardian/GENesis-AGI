"""Full C qualification fixtures; upstream/native edges stay stubbed on host."""

import copy
import importlib.util
import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_controls as controls
    import ego_runtime_cases as cases

    monkeypatch.setattr(controls.owned, "source_pin", lambda: "a" * 40)

    def command(argv, env, root, *args, **kwargs):
        assert argv[:2] == ["/headlong-b/bin/traj", "new"]
        directory = Path(env["TRAJ_DIR"]) / "fixture"
        directory.mkdir()
        (directory / "trajectory.jsonl").write_text(json.dumps({"type": "trajectory", "step_id": REFERENCE}) + "\n")
        return {"returncode": 0, "stdout": REFERENCE + "\n", "stderr": "", "timed_out": False}

    def append(record, env, root):
        row = {**record, "step_id": record.get("step_id", str(uuid.uuid4()))}
        path = Path(env["TRAJ_DIR"]) / "fixture/trajectory.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        return row["step_id"]

    monkeypatch.setattr(controls, "command", command)
    monkeypatch.setattr(controls, "append", append)
    return controls, cases


@pytest.mark.parametrize("name", ["effect", "noop", "refusal", "provider_error", "handler_error", "large_context",
                                  "reactive_noop", "restart_duplicate", "reference_continuity", "observation_failure",
                                  "final_failure", "reference_conflict", "reactive_reference_conflict"])
async def test_shared_mechanics_seed_exact_case_inputs(modules, tmp_path, name):
    controls, cases = modules
    case = cases.case_for(name)
    root = tmp_path / name
    spec = await controls.seed(root, name, mechanics=case)
    assert spec["source_pin"] == "a" * 40 and spec["trajectory_id"] == REFERENCE
    assert len(spec["feed"]) == int(case.reactive)
    assert all(row["target"] == "genesis_ego_cycle" for row in spec["feed"])
    assert len(spec["reference_ids"]) == (3 if case.blocked else 2)
    if case.blocked:
        assert controls.CROSSING in spec["reference_ids"]
        assert controls.CROSSING in (root / "identity/core_identity_prompt.md").read_text()
    if name == "large_context":
        assert (root / "identity/core_identity_prompt.md").stat().st_size > 131072
    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        proposal = await controls.native.ego.get_proposal(db, "proposal")
    assert proposal["status"] == ("approved" if case.kind == "approved" else "pending")
    assert proposal["ego_source"] == "genesis_ego_cycle" and proposal["scope"] == "operate"


@pytest.mark.parametrize("name,defect", [(name, None) for name in (
    "effect", "noop", "refusal", "provider_error", "handler_error", "large_context", "reactive_noop",
    "restart_duplicate", "reference_continuity", "observation_failure", "final_failure", "reference_conflict",
    "reactive_reference_conflict")] + [(name, defect) for name in (
    "handler_error", "observation_failure", "final_failure") for defect in (
    "trigger.json", "context.raw", "stdout.txt", "stderr.txt", "native-run-id", "config-binding",
    "binary-prompt-binding", "child-binding")] + [("observation_failure", "wrong-port-error")])
async def test_native_case_oracle_and_mutants_with_actual_owned_bridge(modules, tmp_path, monkeypatch, name, defect):
    controls, cases = modules
    import ego_owned_matrix as matrix

    loader = importlib.util.spec_from_file_location("owned_fixture_tests", ROOT / "tests/test_scripts/test_ego_owned_loop.py")
    tests = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(tests)
    f = await tests.fixture.__wrapped__(tmp_path, monkeypatch)
    case = cases.case_for(name)
    root_id = str(uuid.uuid4())
    f.spec.update(trajectory_id=root_id, reference_ids=[root_id])
    f.path.write_text(json.dumps({"type": "observation", "source": "fixture", "step_id": root_id,
                                  "content": "fixture-stream-marker"}) + "\n")
    (f.root / "identity/core_identity_prompt.md").write_text(" ".join(controls.MARKERS) + " λ @mention-target.txt\n" +
        ("continuity context " * 9000 if name == "large_context" else ""))
    if case.kind != "approved":
        async with aiosqlite.connect(f.root / "fixture.sqlite3") as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM ego_proposals")
            await controls.native.prepare_case(db, "pending")
    if case.reactive:
        f.spec["feed"] = [{"id": "external-1", "seq": 1, "due": 100, "source": "fixture",
                           "target": "genesis_ego_cycle", "type": "observation", "content": "continuity orbit wake"}]
    original_command, original_turn = f.loop.command, f.bridge.native.native_turn

    def command(argv, env, cwd, data=None):
        if "--redact" in argv and case.blocked:
            return SimpleNamespace(returncode=65, stdout="", stderr="reference_conflict")
        return original_command(argv, env, cwd, data)

    async def turn(native_fixture, trace):
        result, messages, stderr = await original_turn(native_fixture, trace)
        if case.kind == "provider_error":
            result["api_error_status"] = 400
        return result, messages, stderr

    monkeypatch.setattr(f.loop, "command", command)
    monkeypatch.setattr(f.bridge.native, "native_turn", turn)
    kinds = [cases.kind_for(name, second=bool(i)) for i in range(case.wakes)]
    f.spec["responses"] = [{**tests.response(kind, case.fault), "scenario": name,
                           "fixture_header_id": matrix.REFERENCE if name == "reference_continuity" and i == 0 else None}
                          for i, kind in enumerate(kinds)]
    if defect == "wrong-port-error":
        write_effect = controls.native.write_effect

        def fail_after_effect(root):
            write_effect(root)
            raise OSError("unrelated effect sink failure after append")

        monkeypatch.setattr(controls.native, "write_effect", fail_after_effect)
    elif defect:
        collect = f.loop.collect

        async def corrupt_before_collection(root, spec, attempt, run, binding, returncode):
            if defect == "config-binding":
                path = run / "config.json"
                path.write_text(json.dumps({**json.loads(path.read_text()), "unbound-extra": "changed"}))
            elif defect == "binary-prompt-binding":
                with (run / "raw-prompt.bin").open("ab") as stream:
                    stream.write(b"changed")
            elif defect == "child-binding":
                path = run / "child.json"
                path.write_text(json.dumps({**json.loads(path.read_text()), "pid": 99999999}))
            else:
                (run / defect).unlink()
            return await collect(root, spec, attempt, run, binding, returncode)

        monkeypatch.setattr(f.loop, "collect", corrupt_before_collection)
    before = await f.loop.snapshot(f.root)
    reports = [await f.run(1) for _ in kinds]
    trajectory = [json.loads(line) for line in f.path.read_text().splitlines()]
    runs = []
    for directory in sorted(f.root.glob("attempt-*")):
        runs.append({"driver": json.loads((directory / "driver.json").read_text()),
                     "config": json.loads((directory / "config.json").read_text()),
                     "raw_prompt": (directory / "raw-prompt.txt").read_text(),
                     "calls": matrix.ReceiptView(directory / "calls.jsonl").calls,
                     "closed": json.loads((directory / "child-closed.json").read_text())})
    row = {"scenario": name, "manifest": f.spec, "reports": reports, "before": before,
           "after": await f.loop.snapshot(f.root), "runs": runs, "trajectory": trajectory,
           "groups_closed": True, "commands": [{"returncode": 2 if r["status"] in {"uncertain", "blocked"} else 0,
                                                   "timed_out": False} for r in reports],
           "backends": [{"unexpected": [], "main_requests": 0 if case.blocked else 1,
                         "provider_error": case.kind == "provider_error", "captures": [] if case.blocked else
                         [{"body": {"messages": [{"content": f.trace.turns[i]}]}}]} for i in range(case.wakes)],
           "history_lookup": {"returncode": 0}, "history_rows": [r for r in trajectory if r.get("run_id") == runs[0]["driver"]["run_id"]]}
    assert matrix.qualify(case, row) is (defect is None), (name, defect)
    if defect:
        return
    for defect in ("noop", "outcome", "effect", "cursor", "source", "terminal", "cleanup", "timeout"):
        mutant = copy.deepcopy(row)
        if defect == "noop":
            mutant["runs"] = []
        elif defect == "outcome":
            mutant["reports"][-1]["observations"][0]["outcome"] = "false-success"
        elif defect == "effect":
            mutant["after"]["effect"] = "unexpected bytes"
        elif defect == "cursor":
            mutant["reports"][-1]["state"]["cursor"] = 99
        elif defect == "source":
            mutant["runs"][0]["driver"]["outer_source"] = "foreign"
        elif defect == "terminal":
            mutant["runs"][0]["driver"]["complete"] = not mutant["runs"][0]["driver"]["complete"]
        elif defect == "cleanup":
            mutant["groups_closed"] = False
        else:
            mutant["commands"][0]["timed_out"] = True
        assert matrix.qualify(case, mutant) is False, (name, defect)


@pytest.mark.parametrize("fault", [None, "missing_native", "changed_source"])
async def test_actual_matrix_main_keeps_native_and_state_populations_separate(modules, tmp_path, monkeypatch, capsys, fault):
    import ego_owned_matrix as matrix

    sdk_dir = tmp_path / "sdk"
    (sdk_dir / "_bundled").mkdir(parents=True)
    (sdk_dir / "_bundled/claude").write_bytes(b"owned metadata fixture, not executable")
    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(sdk_dir / "__init__.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(matrix, "preflight", lambda: None)
    monkeypatch.setattr(matrix, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=tmp_path))
    monkeypatch.setattr(matrix.importlib.metadata, "version", lambda name: "0.2.172")
    monkeypatch.setattr(matrix.subprocess, "check_output", lambda *args, **kwargs: "owned-fixture-version")
    monkeypatch.setattr(matrix, "dependency_inventory", lambda: {"os": "fixture", "python": "fixture"})
    hashes = []

    def tree_hash(path):
        hashes.append(str(path))
        return "before" if fault != "changed_source" or len(hashes) <= 4 else "after"

    async def scenario(root, name):
        case = matrix.case_for(name)
        runs = [{"driver": {"native": None if case.blocked or (fault == "missing_native" and name == "effect")
                            else {"session_id": str(uuid.uuid4())}}} for _ in range(case.wakes)]
        return {"scenario": name, "control_passed": True, "groups_closed": True, "runs": runs}

    monkeypatch.setattr(matrix, "tree_hash", tree_hash)
    monkeypatch.setattr(matrix, "scenario", scenario)
    result = await matrix.main()
    report = json.loads(capsys.readouterr().out)
    assert result == (0 if fault is None else 1)
    assert report["status"] == ("pass" if fault is None else "incomplete")
    assert len(report["cases"]) == 13 and report["native_sessions"] == (12 if fault == "missing_native" else 13)
    assert sum(len(group) for group in report["state_controls"]["rows"].values()) == 60
    assert len(report["state_controls"]["journals"]) == 60 and len(hashes) == 8
    assert not list(tmp_path.glob("tmp*"))
