"""Real core/SQLite with stub native edges and injected clock; no actual timing claim."""

import asyncio
import base64
import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from genesis.eval.qualification.evidence import Campaign, Incomplete


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    path = Path(__file__).with_name("test_ego_owned_loop.py")
    spec = importlib.util.spec_from_file_location("timing_core_fixture", path)
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    f = await tests.fixture.__wrapped__(tmp_path, monkeypatch)
    import ego_owned_timing as timing

    return SimpleNamespace(f=f, timing=timing, response=tests.response)


async def row_for(fixture, name, offset=None):
    f, t = fixture.f, fixture.timing
    count = 6 if name == "timers" else 3 if offset is not None else 2 if name == "sigterm-backoff" else 1
    kind = "approved" if name == "exclusive-writer" else "echo"
    f.spec["responses"] = [fixture.response(kind) for _ in range(count)]
    if offset is not None or kind == "approved":
        f.spec["feed"] = [{"id":"external-1", "seq":1, "due":100+(offset or 0), "source":"fixture",
                           "target":"genesis_ego_cycle", "type":"observation", "content":"continuity wake"}]
    if kind == "echo":
        async with f.loop.aiosqlite.connect(f.root / "fixture.sqlite3") as db:
            await db.execute("UPDATE ego_proposals SET status = 'pending'")
            await db.commit()
    f.loop.save(f.root / "manifest.json", f.spec)
    commands, reports, details = [], [], {}
    if name == "sigterm-backoff":
        stop = asyncio.Event()

        async def stage(stage, state):
            if stage == "committed":
                details["signal_at"] = f.trace.now
                stop.set()

        reports.append(await f.loop.run(f.root, f.spec, steps=2, stop=stop, clock=lambda:f.trace.now, stage=stage))
    else:
        for steps in ([1,5] if name == "timers" else [count]):
            reports.append(await f.run(steps))
            if name == "timers" and len(reports) == 1:
                details["writer"] = t.recovery.boundary(f.root)
    commands = [{"returncode":0, "timed_out":False} for _ in reports]
    if name == "exclusive-writer":
        with Campaign(f.root / "journal"), pytest.raises(Incomplete) as caught, Campaign(f.root / "journal"):
            pass
        details["contender"] = {"command":{"returncode":2,"timed_out":False},
            "report":{"schema":"ego-owned-loop/v1", "status":"incomplete", "error":f"Incomplete: {caught.value}"},
            "requests_before":0,"requests_after":0,"journal_before":"same", "journal_after":"same"}
    rows = t.completed_journal(f.root)
    if offset is not None:
        details.update(offset=offset, classification=t.classify(offset, f.spec, rows))
    return {"scenario":name,"manifest":f.spec,"reports":reports,"commands":commands,"journal":rows,
            "artifacts":t.recovery.retained(f.root),
            "after":await f.loop.snapshot(f.root),"backends":[{"unexpected":[],"main_requests":2 if kind == "approved" else int(i < len(f.trace.turns))} for i in range(count)],
            "scopes_closed":True, **details}


@pytest.mark.parametrize("name,offset", [("timers",None),("reactive-3",3),("reactive-5",5),("reactive-8",8),
                                        ("sigterm-backoff",None),("exclusive-writer",None)])
async def test_actual_core_rows_and_corruptions(fixture, name, offset):
    t = fixture.timing
    row = await row_for(fixture, name, offset)
    assert t.qualify(name, row)
    assert len(t.calibration(row)) == 4 and all(c["rejected"] for c in t.calibration(row))
    for field, value in [("scopes_closed", False), ("journal", []), ("after", {"effect":"wrong"})]:
        bad = copy.deepcopy(row)
        bad[field] = value
        assert not t.qualify(name, bad)
    for mutate in [lambda r:r["reports"][-1]["state"].update(attempts=True),
                   lambda r:r["commands"][0].update(returncode=2),
                   lambda r:r["backends"][0].update(main_requests=0),
                   lambda r:r["reports"][-1]["state"].update(active={"unpaired":True})]:
        bad = copy.deepcopy(row)
        mutate(bad)
        assert not t.qualify(name, bad)


@pytest.mark.parametrize("fault", ["early_timer","wrong_wake","lost_writer","wrong_class","early_reactive","missed_window","contender_work","contender_journal"])
async def test_clock_and_lifecycle_negative_controls(fixture, fault):
    name,offset = (("reactive-3",3) if fault in {"wrong_class","early_reactive"} else
                   ("sigterm-backoff",None) if fault == "missed_window" else
                   ("exclusive-writer",None) if fault.startswith("contender_") else ("timers",None))
    t = fixture.timing
    row = await row_for(fixture, name, offset)
    assert t.qualify(name, row)
    if fault == "early_timer":
        t.prepared(row["journal"])[1]["started_at"] -= 1
    elif fault == "wrong_wake":
        t.observations(row["journal"])[1]["next"]["wake_at"] += 1
    elif fault == "lost_writer":
        row["writer"]["journal"][0]["data"]["start"] += 1
    elif fault == "wrong_class":
        row["classification"] = "already-pending"
    elif fault == "early_reactive":
        t.prepared(row["journal"])[1]["started_at"] = 99
    elif fault == "missed_window":
        row["signal_at"] = row["reports"][-1]["state"]["wake_at"]
    elif fault == "contender_work":
        row["contender"]["requests_after"] = 1
    else:
        row["contender"]["journal_after"] = "changed"
    assert not t.qualify(name, row)


async def test_preflight_runs_before_clock_or_runtime(fixture, monkeypatch):
    def reject():
        raise ValueError("containment absent")

    monkeypatch.setattr(fixture.timing, "preflight", reject)
    with pytest.raises(ValueError, match="containment absent"):
        await fixture.timing.main()


@pytest.mark.parametrize("fault", ["empty","post_reopen","raw_missing","raw_hash","raw_size","raw_bytes"])
async def test_first_commit_writer_boundary_rejects_corrupt_evidence(fixture, fault):
    t = fixture.timing
    row = await row_for(fixture,"timers")
    assert t.qualify("timers",row)
    if fault == "empty":
        row["writer"]["journal"] = []
    elif fault == "post_reopen":
        row["writer"]["journal"] = copy.deepcopy(row["journal"])
    else:
        artifact, = [a for a in row["writer"]["artifacts"] if a["path"] == "journal/answers.jsonl"]
        if fault == "raw_missing":
            row["writer"]["artifacts"].remove(artifact)
        elif fault == "raw_hash":
            artifact["sha256"] = "0"*64
        elif fault == "raw_size":
            artifact["size"] = True
        else:
            changed = b'{"kind":"synthetic false boundary"}\n'
            artifact.update(content_b64=base64.b64encode(changed).decode(),size=len(changed),sha256=t.fixture.owned.sha(changed))
    assert not t.qualify("timers",row)


async def test_observer_retries_partial_append_only(fixture):
    root, t = fixture.f.root, fixture.timing
    assert t.completed_journal(root) == []
    (root / "journal").mkdir()
    (root / "journal/answers.jsonl").write_text('{"kind":')
    assert t.completed_journal(root) == []
    (root / "journal/answers.jsonl").write_text('{"kind":"fixture"}\n')
    assert t.completed_journal(root) == [{"kind":"fixture"}]


@pytest.mark.parametrize("interrupted", [False,True])
async def test_owned_harmless_command_closes_on_normal_and_exception_paths(fixture, monkeypatch, interrupted):
    t, f = fixture.timing, fixture.f
    program = 'import time; time.sleep(60)' if interrupted else 'print("owned harmless command")'
    monkeypatch.setattr(t, "argv", lambda *a:[sys.executable,"-c",program])
    if interrupted:
        with pytest.raises(RuntimeError, match="own fixture interruption"), t.launched(f.root, f.spec, 1) as child:
            raise RuntimeError("own fixture interruption")
        assert child.returncode == -9 and not t.processes.group_alive(child.pid)
    else:
        with t.launched(f.root, f.spec, 1) as child:
            result = t.finished(child)
        assert result == {"returncode":0,"stdout":"owned harmless command\n","stderr":"","timed_out":False}


@pytest.mark.parametrize("fault", [None,"ownership","predicate","no_backoff","source_drift"])
async def test_actual_controller_main_requires_complete_applicable_population(fixture, monkeypatch, tmp_path, capsys, fault):
    t = fixture.timing
    sdk_root = tmp_path / "sdk"
    (sdk_root / "_bundled").mkdir(parents=True)
    (sdk_root / "_bundled/claude").write_bytes(b"non-executable owned SDK metadata fixture")
    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(sdk_root / "__init__.py")
    monkeypatch.setitem(sys.modules,"claude_agent_sdk",sdk)
    monkeypatch.setattr(t,"preflight",lambda:None)
    monkeypatch.setattr(t.processes,"init_identity",lambda controller:{"controller":2,"parent":1,"script":controller})
    monkeypatch.setattr(t,"TemporaryDirectory",lambda **kw:tempfile.TemporaryDirectory(dir=tmp_path))
    monkeypatch.setattr(t.fixture.owned,"source_pin",lambda:"a"*40)
    monkeypatch.setattr(t.importlib.metadata,"version",lambda name:"0.2.172")
    monkeypatch.setattr(t.subprocess,"check_output",lambda *a,**kw:"owned fixture version")
    monkeypatch.setattr(t,"dependency_inventory",lambda:{"os":"fixture","python":"fixture"})
    hashes=[]

    def tree_hash(path):
        hashes.append(str(path))
        return "after" if fault == "source_drift" and len(hashes)>4 else "before"

    async def scenario(root,name,offset):
        root.mkdir()
        assert json.loads((root.parent / "population.json").read_text()) == t.population()
        if fault == "ownership":
            raise t.processes.OwnershipError("unaccounted owned fixture descendant")
        return {"scenario":name,"passed":fault != "predicate",
                "classification":"already-pending" if fault == "no_backoff" else "during-backoff"}

    monkeypatch.setattr(t,"tree_hash",tree_hash)
    monkeypatch.setattr(t,"scenario",scenario)
    monkeypatch.setattr(t,"calibration",lambda row:[{"rejected":True} for _ in range(4)])
    result=await t.main()
    report=json.loads(capsys.readouterr().out)
    assert result == (0 if fault is None else 1)
    assert report["status"] == ("pass" if fault is None else "incomplete") and len(hashes)==8
    assert len(report["cases"]) == (0 if fault == "ownership" else 1 if fault == "predicate" else 6)
    if fault is None:
        assert len(report["calibration"]) == 24
    if fault == "ownership":
        assert report["challenge"]["material_scope_challenge"] is True
    if fault == "no_backoff":
        assert "no fixed arrival" in report["challenge"]["error"]
