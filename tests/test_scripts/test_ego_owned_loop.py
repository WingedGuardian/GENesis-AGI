"""Actual owned-loop/bridge main, disposable CRUD, fake native/Headlong edges."""

import asyncio
import contextlib
import io
import json
import os
import signal
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import aiosqlite
import pytest

from genesis.db.schema import TABLES

ROOT = Path(__file__).resolve().parents[2]
ID = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_loop as loop
    import headlong_native_bridge as bridge

    root = tmp_path / "case"
    identity = root / "identity"
    for sub in ("run", "workdir", "memories", "skills/fixture", "kernel", "trajectories/fixture"):
        (identity / sub).mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir()
    path = identity / "trajectories/fixture/trajectory.jsonl"
    path.write_text(json.dumps({"type": "observation", "source": "fixture", "step_id": ID, "content": "seed continuity"}) + "\n")
    for name in ("core_identity_prompt.md", "skills/fixture/SKILL.md"):
        (identity / name).write_text("whole synthetic context λ @mention-target.txt")
    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        for table in ("ego_proposals", "ego_proposal_revisions"):
            await db.execute(TABLES[table])
        await bridge.native.prepare_case(db, "approved")
    spec = {"root": str(root), "executor": "claude-code", "model": "sonnet", "ego_source": "genesis_ego_cycle",
            "source_pin": "a" * 40, "sources": loop.source_hashes(), "trajectory_id": ID,
            "reference_ids": [ID], "start": 100, "feed": [], "responses": [],
            "pacing": {"base": 5, "factor": 2, "cap": 40, "hold": 1}}
    trace = SimpleNamespace(turns=[], argv=[], now=100, mutation=None, fail_stage=None)

    def append(record, cwd):
        record = {**record, "step_id": record.get("step_id", str(uuid.uuid4()))}
        with path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        return record["step_id"]

    def command(argv, env, cwd, data=None):
        if argv[0] == "/headlong-b/bin/traj":
            return SimpleNamespace(returncode=0, stdout=append(json.loads(data), cwd), stderr="")
        if "--redact" in argv:
            return SimpleNamespace(returncode=0, stdout=Path(env["OWNED_RAW_PROMPT"]).read_text(), stderr="")
        Path(env["OWNED_RELATED_FILE"]).write_text("fixture-memory\n")
        return SimpleNamespace(returncode=0, stdout=(identity / "core_identity_prompt.md").read_text() + path.read_text(), stderr="")

    async def turn(native_fixture, native_trace):
        trace.turns.append(native_fixture.prompt)
        kind = native_fixture.kind
        if kind not in {"echo", "provider_error"}:
            # Real SDK converts tool exceptions to messages.
            with contextlib.suppress(RuntimeError, OSError):
                await native_fixture.port.call({"proposal_id": "proposal"})
        terminal = {"is_error": kind == "provider_error", "subtype": "error" if kind == "provider_error" else "success",
                    "result": f"native-final-{kind}", "session_id": str(uuid.uuid4())}
        messages = [{"type": "ResultMessage", "data": terminal}, {"type": "SystemMessage", "data": {
            "subtype": "init", "data": {"tools": ["mcp__fixture__claim_effect"],
                                          "model": "fixture-resolved-model", "session_id": terminal["session_id"]}}}]
        return terminal, messages, []

    async def invoke(argv, env, run, stop):
        trace.argv.append(argv)
        with monkeypatch.context() as child, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            child.setattr(sys, "argv", argv[1:])
            for key, value in env.items():
                child.setenv(key, value)
            code = await bridge.main(tmp_path)
        (run / "stdout.txt").write_text(out.getvalue())
        (run / "stderr.txt").write_text(err.getvalue())
        loop.save(run / "child.json", {"pid": 12345, "pgid": 12345})
        loop.save(run / "child-closed.json", {"pid": 12345, "pgid": 12345, "returncode": code, "group_alive": False})
        if trace.mutation:
            trace.mutation(run)
        return code

    async def sleep(delay):
        trace.now += delay

    async def stage(name, state):
        if trace.fail_stage == name:
            raise OSError("owned interruption " + name)

    async def run(steps):
        return await loop.run(root, spec, steps=steps, clock=lambda: trace.now, sleep=sleep, stage=stage)

    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(root / "owned-sdk-stub.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(bridge, "preflight", lambda: None)
    monkeypatch.setattr(bridge, "append_step", append)
    monkeypatch.setattr(bridge.native, "native_turn", turn)
    monkeypatch.setattr(loop, "command", command)
    monkeypatch.setattr(loop, "invoke", invoke)
    return SimpleNamespace(root=root, spec=spec, trace=trace, loop=loop, bridge=bridge, run=run, path=path)


def response(kind="echo", fault=None):
    return {"kind": kind, "backend_port": 12345, "scenario": "noop" if kind == "echo" else "effect",
            "fault": fault, "fixture_header_id": None}


async def test_actual_loop_empty_feed_runs_timers_and_reopens_without_replaying(fixture):
    f = fixture
    f.spec["responses"] = [response() for _ in range(4)]
    first = await f.run(3)
    assert first["state"]["cursor"] == 0 and first["state"]["timer_done"] == 3
    assert first["state"]["wake_at"] == 135 and len(f.trace.turns) == 3
    second = await f.run(1)
    assert second["state"]["timer_done"] == 4 and len(f.trace.turns) == 4
    assert not (f.root / "effect.bin").exists()


async def test_actual_loop_committed_effect_and_second_claim_are_independent(fixture):
    f = fixture
    f.spec["responses"] = [response("approved"), response("pending")]
    first = await f.run(1)
    second = await f.run(1)
    assert [o["outcome"] for o in second["observations"]] == ["completed", "refused"]
    assert (f.root / "effect.bin").read_bytes() == f.loop.EFFECT
    prior = first["observations"][0]["evidence"]["references"]
    assert all(identifier in f.trace.turns[-1] for identifier in prior if identifier)
    assert len(f.trace.turns) == 2
    assert second["state"]["related"] == ["fixture-memory"]


@pytest.mark.parametrize("fault", ["observation", "final", "handler_error"])
async def test_partial_effect_or_handler_error_stops_automatic_replay(fixture, fault):
    f = fixture
    f.spec["responses"] = [response("handler_error" if fault == "handler_error" else "approved",
                                    None if fault == "handler_error" else fault), response("approved")]
    first = await f.run(2)
    second = await f.run(2)
    assert first["status"] == second["status"] == "uncertain" and len(f.trace.turns) == 1
    assert first["state"]["cursor"] == 0 and first["state"]["timer_done"] == 0
    assert (f.root / "effect.bin").exists() is (fault != "handler_error")


@pytest.mark.parametrize("stage", ["prepared", "bound", "collected", "committed"])
async def test_interrupted_real_loop_reopens_only_after_committed_observation(fixture, stage):
    f = fixture
    f.spec["responses"] = [response(), response()]
    f.trace.fail_stage = stage
    with pytest.raises(OSError, match="owned interruption"):
        await f.run(1)
    f.trace.fail_stage = None
    result = await f.run(1)
    assert result["status"] == ("budget" if stage == "committed" else "uncertain")
    assert len(f.trace.turns) == (0 if stage in {"prepared", "bound"} else 2 if stage == "committed" else 1)


@pytest.mark.parametrize("artifact", ["config.json", "driver.json", "calls.jsonl", "raw-prompt.bin", "raw-prompt.txt", "child-closed.json"])
async def test_changed_child_binding_or_receipts_cannot_commit(fixture, artifact):
    f = fixture
    f.spec["responses"] = [response("approved")]

    def mutate(run):
        p = run / artifact
        if artifact.startswith("raw-prompt"):
            p.write_text("changed captured prompt")
        elif artifact == "calls.jsonl":
            p.write_text("{\"claim\":")
        else:
            data = json.loads(p.read_text())
            data["group_alive" if artifact == "child-closed.json" else "invocation_id"] = "f" * 32
            p.write_text(json.dumps(data))

    f.trace.mutation = mutate
    result = await f.run(1)
    assert result["status"] == "uncertain" and result["state"]["timer_done"] == 0


@pytest.mark.parametrize("mutation", ["model", "tools", "result", "final_id", "resolved_model", "header_wake", "final_content"])
async def test_unbound_native_terminal_cannot_commit(fixture, mutation):
    f = fixture
    f.spec["responses"] = [response()]

    def mutate(run):
        p = run / "driver.json"
        data = json.loads(p.read_text())
        if mutation == "model":
            data["model"] = "different-model"
        elif mutation == "tools":
            data["messages"][1]["data"]["data"]["tools"] = ["Bash"]
        elif mutation == "result":
            data["messages"][0]["data"]["session_id"] = str(uuid.uuid4())
        elif mutation == "resolved_model":
            data["messages"][1]["data"]["data"].pop("model")
        elif mutation in {"header_wake", "final_content"}:
            rows = [json.loads(line) for line in f.path.read_text().splitlines()]
            for row in rows:
                if row["type"] == ("shellm-run" if mutation == "header_wake" else "final"):
                    row["wake" if mutation == "header_wake" else "content"] = "misbound child evidence"
            f.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        else:
            data["final_id"] = "not-a-canonical-id"
        p.write_text(json.dumps(data))

    f.trace.mutation = mutate
    result = await f.run(1)
    assert result["status"] == "uncertain" and result["state"]["timer_done"] == 0


@pytest.mark.parametrize("artifact", ["stdout.txt", "stderr.txt", "context.raw", "trigger.json", "native-run-id"])
async def test_missing_required_child_artifacts_cannot_commit(fixture, artifact):
    f = fixture
    f.spec["responses"] = [response()]
    f.trace.mutation = lambda run: (run / artifact).unlink()
    result = await f.run(1)
    assert result["status"] == "uncertain" and result["state"]["timer_done"] == 0


async def test_stop_after_binding_never_launches_child(fixture):
    f = fixture
    f.spec["responses"] = [response()]
    stop = asyncio.Event()

    async def stage(name, state):
        if name == "bound":
            stop.set()

    result = await f.loop.run(f.root, f.spec, steps=1, stop=stop, stage=stage, clock=lambda: 100)
    assert result["status"] == "uncertain" and not f.trace.turns


async def test_actual_cli_entrypoint_emits_loop_evidence(fixture, monkeypatch, capsys):
    f = fixture
    f.spec["responses"] = [response()]
    (f.root / "manifest.json").write_text(json.dumps(f.spec))
    monkeypatch.setattr(f.loop, "preflight", lambda: None)
    monkeypatch.setattr(f.loop, "source_pin", lambda: f.spec["source_pin"])
    monkeypatch.setattr(sys, "argv", ["ego_owned_loop.py", "--root", str(f.root), "--steps", "1"])
    assert await f.loop.main(f.root.parent) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"]["timer_done"] == 1 and len(f.trace.turns) == 1


async def test_provider_error_retains_logical_timer_and_retries_fresh_attempt(fixture):
    f = fixture
    f.spec["responses"] = [response("provider_error"), response()]
    first = await f.run(1)
    assert first["state"]["timer_done"] == 0 and first["state"]["retry"]["event"]["id"] == "timer:1"
    second = await f.run(1)
    assert second["state"]["timer_done"] == 1 and second["state"]["timer_reserved"] == 1
    observations = second["observations"]
    assert observations[0]["attempt_id"] != observations[1]["attempt_id"]
    assert [r["outcome"] for r in observations] == ["retry", "noop"]


@pytest.mark.parametrize("count", [1, 15, 16, 17])
async def test_actual_loop_burst_preserves_each_distinct_external_event(fixture, count):
    f = fixture
    f.spec["feed"] = [{"id": f"event-{i}", "seq": i, "due": 100, "source": "fixture",
                       "target": "genesis_ego_cycle", "type": "observation", "content": "same summary"}
                      for i in range(1, count + 1)]
    f.spec["responses"] = [response() for _ in range(count)]
    result = await f.run(count)
    assert result["state"]["cursor"] == count and result["state"]["timer_reserved"] == 0
    triggers = [row for row in map(json.loads, f.path.read_text().splitlines()) if "input_event_id" in row]
    assert [t["input_event_id"] for t in triggers] == [e["id"] for e in f.spec["feed"]]


async def test_changed_retained_history_stops_continuity(fixture):
    f = fixture
    f.spec["responses"] = [response(), response()]
    first = await f.run(1)
    old = f.root / first["observations"][0]["evidence"]["artifacts"][0]["path"]
    old.write_text("changed retained history")
    with pytest.raises(ValueError, match="changed C1 artifact"):
        await f.run(1)
    assert len(f.trace.turns) == 1


async def test_stop_during_backoff_is_prompt(fixture):
    f = fixture
    f.spec["responses"] = [response(), response()]
    await f.run(1)
    stop = asyncio.Event()
    task = asyncio.create_task(f.loop.run(f.root, f.spec, steps=1, stop=stop, clock=lambda: 100))
    await asyncio.sleep(0.02)
    stop.set()
    try:
        result = await asyncio.wait_for(asyncio.shield(task), 0.3)
        assert result["status"] == "stopped" and len(f.trace.turns) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_exhausted_fixture_response_program_never_waits_for_another_timer(fixture):
    f = fixture
    f.spec["responses"] = [response()]
    await f.run(1)
    result = await asyncio.wait_for(f.loop.run(f.root, f.spec, steps=1, clock=lambda: 100), 0.3)
    assert result["status"] == "budget" and len(f.trace.turns) == 1


async def test_preexisting_stop_never_spawns_harmless_child(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_loop as loop

    stop = asyncio.Event()
    stop.set()
    (tmp_path / "workdir").mkdir()
    with pytest.raises(RuntimeError, match="before launch"):
        await loop.invoke([sys.executable, "-c", "raise AssertionError('child must never launch')"],
                          {"IDENTITY_DIR": str(tmp_path), "PATH": os.environ["PATH"]}, tmp_path, stop)


async def test_owned_child_is_cleaned_when_initial_receipt_write_fails(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_loop as loop

    created = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        created.append(process)
        return process

    def fail(*args):
        raise OSError("owned initial child receipt failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(loop, "save", fail)
    identity = tmp_path / "identity"
    (identity / "workdir").mkdir(parents=True)
    try:
        with pytest.raises(OSError, match="initial child"):
            await loop.invoke([sys.executable, "-c", "import time; time.sleep(30)"],
                              {"IDENTITY_DIR": str(identity), "PATH": os.environ["PATH"]}, tmp_path, asyncio.Event())
        assert created and created[0].returncode is not None
        assert not loop.group_alive(created[0].pid)
    finally:
        for process in created:
            if loop.group_alive(process.pid):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
