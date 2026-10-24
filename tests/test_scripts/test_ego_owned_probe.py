"""Recovery wrapper exercises actual core/bridge; no native executable on host."""

import contextlib
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    loader = importlib.util.spec_from_file_location("owned_probe_fixture", ROOT / "tests/test_scripts/test_ego_owned_loop.py")
    tests = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(tests)
    f = await tests.fixture.__wrapped__(tmp_path, monkeypatch)
    import ego_owned_probe as probe

    monkeypatch.setattr(probe, "preflight", lambda: None)
    monkeypatch.setattr(f.loop, "preflight", lambda: None)
    monkeypatch.setattr(f.loop, "source_pin", lambda: f.spec["source_pin"])
    f.spec["feed"] = [{"id": "external-1", "seq": 1, "due": 100, "source": "fixture",
                       "target": "genesis_ego_cycle", "type": "observation", "content": "continuity wake"}]
    f.spec["responses"] = [tests.response("approved")]
    f.loop.save(f.root / "manifest.json", f.spec)

    async def invoke(argv, env, run, stop):
        f.trace.argv.append(argv)
        with monkeypatch.context() as child, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            child.setattr(sys, "argv", argv[1:])
            for key, value in env.items():
                child.setenv(key, value)
            try:
                code = (await probe.main(tmp_path) if argv[1].endswith("ego_owned_probe.py")
                        else await f.bridge.main(tmp_path))
            except OSError:
                code = 2
        (run / "stdout.txt").write_text(out.getvalue())
        (run / "stderr.txt").write_text(err.getvalue())
        f.loop.save(run / "child.json", {"pid": 12345, "pgid": 12345})
        f.loop.save(run / "child-closed.json", {"pid": 12345, "pgid": 12345, "returncode": code, "group_alive": False})
        return code

    monkeypatch.setattr(f.loop, "invoke", invoke)
    return f, probe


@pytest.mark.parametrize("phase", [None, "db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush"])
async def test_actual_wrapper_late_faults_preserve_effect_without_success_or_replay(fixture, monkeypatch, phase):
    f, probe = fixture
    import ego_owned_recovery as recovery
    selected = {"root": str(f.root), "source_pin": f.spec["source_pin"], "sources": f.spec["sources"],
                "mode": "pass" if phase is None else "late", "stage": phase}
    if phase:
        f.spec["responses"].append(dict(f.spec["responses"][0]))
        (f.root / "manifest.json").write_text(json.dumps(f.spec))
    f.loop.save(f.root / "control.json", selected)
    before = await f.loop.snapshot(f.root)
    monkeypatch.setattr(sys, "argv", ["ego_owned_probe.py", "--root", str(f.root), "--steps", "1"])
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = await probe.main(f.root.parent)
    report = json.loads(output.getvalue())
    assert code == (0 if phase is None else 2)
    assert report["state"]["cursor"] == int(phase is None)
    assert report["status"] == ("budget" if phase is None else "uncertain")
    attempt = f.root / "attempt-00001"
    assert (attempt / "driver.json").exists() is (phase is None)
    assert (f.root / "effect.bin").read_bytes() == f.loop.EFFECT
    native = json.loads((attempt / "probe-native.json").read_text())
    assert native["native"]["is_error"] is False and native["native"]["result"] == "native-final-approved"
    rows = [json.loads(v) for v in f.path.read_text().splitlines()]
    assert len([r for r in rows if r["type"] == "observation" and r.get("run_id")]) == 1
    assert len([r for r in rows if r["type"] == "final"]) == 1
    if phase:
        assert json.loads((f.root / "probe-late.json").read_text())["fault"] == phase
    if phase in {"driver_short", "driver_close", "rename"}:
        pending = (attempt / "driver.pending").read_bytes()
        assert (len(pending) == 3) is (phase == "driver_short")
    writer = recovery.boundary(f.root)
    reopened = await f.loop.run(f.root, f.spec, steps=1)
    assert reopened["state"] == report["state"] and len(f.trace.turns) == 1
    assert (f.root / "effect.bin").read_bytes() == f.loop.EFFECT
    pending_path = attempt / "driver.pending"
    pending_bytes = pending_path.read_bytes() if pending_path.exists() else b""
    row = {"phase": phase, "marker": json.loads((f.root / "probe-late.json").read_text()) if phase else None,
           "native": native, "manifest": f.spec, "before": before, "after": await f.loop.snapshot(f.root),
           "reports": [report, reopened], "commands": [{"returncode": code, "timed_out": False}] * 2,
           "writer": writer, "journal": writer["journal"],
           "backend": {"main_requests": 2, "unexpected": []}, "requests_before_reopen": 2,
           "scopes_closed": True, "published": (attempt / "driver.json").exists(),
           "calls": f.bridge.ReceiptView(attempt / "calls.jsonl").calls,
           "child": json.loads((attempt / "child.json").read_text()),
           "closed": json.loads((attempt / "child-closed.json").read_text()),
           "trajectory": rows, "pending_size": len(pending_bytes),
           "pending": json.loads(pending_bytes) if phase in {"driver_close", "rename"} else None}
    # Backend/session edges are synthetic here; actual native counts belong to CI.
    if phase is None:
        assert recovery.qualify_ordinary(row) is True
    else:
        assert recovery.qualify_late(phase, row) is True
        for defect in ("collection", "call", "cursor", "cleanup", "reason", "pending", "writer"):
            mutant = copy.deepcopy(row)
            if defect == "collection":
                mutant["reports"][0]["observations"][0]["evidence"]["errors"] = ["unrelated missing context"]
            elif defect == "call":
                mutant["calls"][0]["claim"] = 1
            elif defect == "cursor":
                mutant["reports"][0]["state"]["cursor"] = False
            elif defect == "cleanup":
                mutant["closed"]["group_alive"] = True
            elif defect == "reason":
                mutant["marker"]["fault"] = "unrelated"
            elif defect == "writer":
                mutant["writer"]["journal"] = []
            elif phase in {"driver_close", "rename"}:
                mutant["pending"]["invocation_id"] = "f" * 32
            else:
                mutant["pending_size"] = 99
            assert recovery.qualify_late(phase, mutant) is False, (phase, defect)


@pytest.mark.parametrize("stage", ["before_intake", "after_intake", "prepared", "trigger", "config", "bound", "claimed", "effect", "receipt", "collected", "committed"])
async def test_actual_wrapper_reaches_each_local_stage_once(fixture, monkeypatch, stage):
    f, probe = fixture
    selected = {"root": str(f.root), "source_pin": f.spec["source_pin"], "sources": f.spec["sources"], "mode": "crash", "stage": stage}
    if stage not in {"before_intake", "after_intake", "committed"}:
        f.spec["responses"].append(dict(f.spec["responses"][0]))
        (f.root / "manifest.json").write_text(json.dumps(f.spec))
    f.loop.save(f.root / "control.json", selected)
    (f.root / "release").touch()
    monkeypatch.setattr(sys, "argv", ["ego_owned_probe.py", "--root", str(f.root), "--steps", "1"])
    with contextlib.redirect_stdout(io.StringIO()):
        code = await probe.main(f.root.parent)
    assert code == 0
    marker = json.loads((f.root / ("probe-" + stage + ".json")).read_text())
    assert marker["stage"] == stage and marker["root"] == str(f.root)
    assert len(f.trace.turns) == 1 and (f.root / "effect.bin").read_bytes() == f.loop.EFFECT


@pytest.mark.parametrize("defect", ["root", "source_pin", "sources", "mode", "stage", "extra"])
async def test_invalid_control_fails_before_core_mutation(fixture, monkeypatch, defect):
    f, probe = fixture
    selected = {"root": str(f.root), "source_pin": f.spec["source_pin"], "sources": f.spec["sources"], "mode": "pass", "stage": None}
    selected[defect] = "foreign"
    f.loop.save(f.root / "control.json", selected)
    monkeypatch.setattr(sys, "argv", ["ego_owned_probe.py", "--root", str(f.root), "--steps", "1"])
    with pytest.raises(ValueError, match="control"):
        await probe.main(f.root.parent)
    assert not (f.root / "journal").exists() and f.trace.turns == []


async def test_preflight_runs_before_control_or_root_reads(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_probe as probe

    def reject():
        raise ValueError("containment missing")

    monkeypatch.setattr(probe, "preflight", reject)
    monkeypatch.setattr(sys, "argv", ["probe", "--native"])
    with pytest.raises(ValueError, match="containment"):
        await probe.main()


async def test_parent_wrapper_accepts_explicit_default_stage(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_probe as probe

    async def original(*args, **kwargs):
        return 7

    monkeypatch.setattr(probe.owned, "run", original)
    with probe.parent_hooks(tmp_path, {"mode": "pass", "stage": None}):
        assert await probe.owned.run(stage=None) == 7


@pytest.mark.parametrize("stage", ["before_intake", "after_intake", "prepared", "trigger", "config", "bound",
                                  "native", "claimed", "effect", "receipt", "collected", "committed"])
async def test_crash_scorer_with_actual_journal_and_exception_seams(fixture, monkeypatch, stage):
    """Exception/stub oracle only; actual SIGKILL/native/cleanup proof belongs to CI."""
    f, probe = fixture
    import ego_owned_recovery as recovery

    class Interrupted(BaseException):
        pass

    selected = {"root": str(f.root), "source_pin": f.spec["source_pin"], "sources": f.spec["sources"], "mode": "crash", "stage": stage}
    if stage not in {"before_intake", "after_intake", "committed"}:
        f.spec["responses"].append(dict(f.spec["responses"][0]))
        (f.root / "manifest.json").write_text(json.dumps(f.spec))
    f.loop.save(f.root / "control.json", selected)

    def interrupt(root, selected, name, **details):
        if selected["mode"] == "crash" and selected["stage"] == name:
            probe.mark(root, name, **details)
            raise Interrupted(name)

    monkeypatch.setattr(probe, "pause", interrupt)
    original_turn = f.bridge.native.native_turn

    async def native_barrier(native_fixture, trace):
        if stage == "native":
            interrupt(f.root, selected, "native")
        return await original_turn(native_fixture, trace)

    monkeypatch.setattr(f.bridge.native, "native_turn", native_barrier)
    monkeypatch.setattr(sys, "argv", ["ego_owned_probe.py", "--root", str(f.root), "--steps", "1"])
    before = await f.loop.snapshot(f.root)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(Interrupted):
        await probe.main(f.root.parent)
    after_kill = await f.loop.snapshot(f.root)
    writer = recovery.boundary(f.root)
    monkeypatch.setattr(probe, "pause", lambda *args, **kwargs: None)
    monkeypatch.setattr(f.bridge.native, "native_turn", original_turn)
    reopened = await f.loop.run(f.root, f.spec, steps=1)
    resumable = stage in {"before_intake", "after_intake"}
    # Synthetic request/exit/closure fields calibrate the predicate; they are not
    # observations of native execution or real process interruption.
    requests = (0 if stage in {"before_intake", "after_intake", "prepared", "trigger", "config", "bound"}
                else 1 if stage in {"native", "claimed", "effect"} else 2)
    row = {"stage": stage, "manifest": f.spec, "before": before, "after_kill": after_kill,
           "after": await f.loop.snapshot(f.root), "reopened": reopened,
           "marker": json.loads((f.root / ("probe-" + stage + ".json")).read_text()),
           "killed": {"returncode": -9, "timed_out": False}, "reopen_command": {
               "returncode": 0 if reopened["status"] == "budget" else 2, "timed_out": False},
           "journal": recovery.journal(f.root), "writer": writer, "requests_before_reopen": requests,
           "requests_after_reopen": requests + 2 * int(resumable), "backend": {"unexpected": []}, "scopes_closed": True}
    assert recovery.qualify_crash(stage, row) is True, stage
    for defect in ("effect", "cursor", "outcome", "cleanup", "requests", "reason", "timer", "state", "capacity",
                   "writer", "writer_config", "writer_driver", "writer_trigger", "reader_prefix"):
        mutant = copy.deepcopy(row)
        if defect == "effect":
            mutant["after"]["effect"] = "unexpected" if row["after"]["effect"] is None else None
        elif defect == "cursor":
            mutant["reopened"]["state"]["cursor"] = bool(row["reopened"]["state"]["cursor"])
        elif defect == "outcome":
            mutant["reopened"]["status"] = "uncertain" if reopened["status"] == "budget" else "budget"
        elif defect == "cleanup":
            mutant["scopes_closed"] = False
        elif defect == "requests":
            mutant["requests_after_reopen"] += 1
        elif defect == "reason":
            mutant["marker"]["stage"] = "unrelated"
        elif defect == "timer":
            mutant["reopened"]["state"]["timer_reserved"] = True
        elif defect == "state":
            mutant["after"]["state"]["ego_source"] = "foreign"
        elif defect == "capacity":
            mutant["manifest"]["responses"] = []
        elif defect == "writer":
            mutant["writer"]["journal"] = []
        elif defect == "writer_config":
            mutant["writer"]["config"] = {"foreign": True} if writer["config"] is None else None
        elif defect == "writer_driver":
            mutant["writer"]["driver"] = {"foreign": True} if writer["driver"] is None else None
        elif defect == "writer_trigger":
            mutant["writer"]["trajectory"].append({"type": "observation", "input_event_id": "external-1"})
        else:
            mutant["journal"] = mutant["journal"][1:]
        assert recovery.qualify_crash(stage, mutant) is False, (stage, defect)
