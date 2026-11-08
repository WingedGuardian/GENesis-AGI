"""Controller wiring uses explicit stub edges; no host native/process-crash claim."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ci"))
    import ego_owned_recovery

    return ego_owned_recovery


@pytest.mark.parametrize("fault", [None, "ownership", "crash_predicate", "noop_instrument", "source_drift"])
async def test_actual_controller_main_fails_incomplete_populations(module, monkeypatch, tmp_path, capsys, fault):
    sdk_root = tmp_path / "sdk"
    (sdk_root / "_bundled").mkdir(parents=True)
    (sdk_root / "_bundled/claude").write_bytes(b"non-executable owned SDK metadata fixture")
    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(sdk_root / "__init__.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(module, "preflight", lambda: None)
    monkeypatch.setattr(module.processes, "init_identity", lambda: {"controller": 2, "parent": 1})
    monkeypatch.setattr(module, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=tmp_path))
    monkeypatch.setattr(module.fixture.owned, "source_pin", lambda: "a" * 40)
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "0.2.172")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "owned fixture version")
    monkeypatch.setattr(module, "dependency_inventory", lambda: {"os": "fixture", "python": "fixture"})
    calls, hashes = [], []

    def tree_hash(path):
        hashes.append(str(path))
        return "after" if fault == "source_drift" and len(hashes) > 4 else "before"

    def adoption(root):
        root.mkdir()
        assert json.loads((root.parent / "population.json").read_text()) == module.population()
        calls.append("adoption")
        if fault == "ownership":
            raise module.processes.OwnershipError("unaccounted fixture descendant")
        return {"passed": True, "missing_closure_rejected": True}

    async def late(root, phase, kind="approved"):
        root.mkdir()
        calls.append(kind if phase is None else phase)
        return {"phase": phase, "passed": True if fault == "noop_instrument" else kind == "approved"}

    async def crash(root, stage):
        root.mkdir()
        calls.append(stage)
        return {"stage": stage, "passed": fault != "crash_predicate"}

    monkeypatch.setattr(module, "tree_hash", tree_hash)
    monkeypatch.setattr(module, "adoption", adoption)
    monkeypatch.setattr(module, "late", late)
    monkeypatch.setattr(module, "crash", crash)
    result = await module.main()
    report = json.loads(capsys.readouterr().out)
    assert result == (0 if fault is None else 1)
    assert report["status"] == ("pass" if fault is None else "incomplete")
    assert len(report["declared_population"]["crashes"]) == 12
    assert len(report["declared_population"]["late_io"]) == 7 and len(hashes) == 8
    if fault in {"ownership", "noop_instrument"}:
        assert report["crashes"] == report["late_io"] == []
    elif fault == "crash_predicate":
        assert len(report["crashes"]) == 1 and report["late_io"] == []
    else:
        assert len(report["crashes"]) == 12 and len(report["late_io"]) == 7
    if fault == "ownership":
        assert report["challenge"]["material_scope_challenge"] is True and calls == ["adoption"]
    assert not list(tmp_path.glob("tmp*"))


async def test_controller_preflight_precedes_runtime_work(module, monkeypatch):
    def reject():
        raise ValueError("containment missing")

    monkeypatch.setattr(module, "preflight", reject)
    with pytest.raises(ValueError, match="containment"):
        await module.main()


def test_first_backend_response_is_held_until_controller_release(module, monkeypatch, tmp_path):
    run = tmp_path / "attempt-00001"
    run.mkdir()
    (run / "config.json").write_text(json.dumps({"invocation_id": "a" * 32}))

    def response(backend, path, payload):
        backend.evidence["main_requests"] += 1
        return 200, b"owned response"

    monkeypatch.setattr(module.Backend, "respond", response)
    answers = []
    with module.HeldBackend("approved", None, tmp_path, True) as backend:
        worker = threading.Thread(target=lambda: answers.append(backend.respond("/v1/messages", {})))
        worker.start()
        try:
            marker = tmp_path / "probe-native.json"
            module.processes.wait_for(marker.exists)
            assert worker.is_alive() and answers == []
            assert json.loads(marker.read_text())["owner"] == "backend"
        finally:
            backend.release.set()
            worker.join(timeout=60)
        assert not worker.is_alive() and answers == [(200, b"owned response")]
        assert backend.respond("/v1/messages", {}) == (200, b"owned response")
        assert backend.evidence["main_requests"] == 2


def test_adoption_oracle_rejects_missing_reaping_and_wrong_parent(module):
    row = {"outer": 3, "child": 4, "before": {"4": {"ppid": 3}},
           "adopted": {"4": {"ppid": 1, "pgid": 4}}, "after": {"1": {"pgid": 1}, "2": {"pgid": 2}}, "closed": True}
    assert module.qualify_adoption(row) is True
    assert module.qualify_adoption({**row, "after": row["adopted"]}) is False
    assert module.qualify_adoption({**row, "closed": False}) is False
    row["adopted"]["4"]["ppid"] = 3
    assert module.qualify_adoption(row) is False


@pytest.mark.parametrize("interrupted", [False, True])
def test_adoption_reaps_owner_then_closes_pipes_on_success_or_error(module, monkeypatch, tmp_path, interrupted):
    """Protocol stub supplements the real inherited-writer regression."""
    state = {"created": False, "exited": False, "reaped": False, "child_closed": False, "waits": 0}
    events = []
    controller = os.getpid()

    def census():
        rows = {1: {"pid": 1, "ppid": 0, "pgid": 1, "sid": 1},
                controller: {"pid": controller, "ppid": 1, "pgid": controller, "sid": controller}}
        if state["created"] and not state["reaped"]:
            rows[3] = {"pid": 3, "ppid": controller, "pgid": 3, "sid": 3}
        if state["created"] and not state["child_closed"]:
            rows[4] = {"pid": 4, "ppid": 1 if state["exited"] else 3, "pgid": 4, "sid": 4}
        return rows

    class Outer:
        pid = 3
        returncode = None

        def __init__(self, argv, **kwargs):
            state["created"] = True
            Path(argv[-2]).write_text(json.dumps({"outer": 3, "child": 4, "nonce": argv[-1]}))

        def poll(self):
            return -9 if state["exited"] else None

        def wait(self, **kwargs):
            assert state["exited"]
            events.append("reaped-owner")
            state["reaped"] = True
            self.returncode = -9
            return -9

        def communicate(self, **kwargs):
            if not state["child_closed"]:
                raise RuntimeError("inherited writer still alive")
            self.wait()
            events.append("drained")
            return "", ""

    def killpg(pgid, sig):
        alive = not state["exited"] if pgid == 3 else not state["child_closed"]
        if not alive:
            raise ProcessLookupError
        if sig:
            events.append("kill-owner" if pgid == 3 else "close-child")
            state["exited" if pgid == 3 else "child_closed"] = True

    def wait_for(predicate, process=None, **kwargs):
        state["waits"] += 1
        if interrupted and state["waits"] == 2:
            raise RuntimeError("calibration interrupted")
        assert predicate()

    monkeypatch.setattr(module.subprocess, "Popen", Outer)
    monkeypatch.setattr(module.processes, "census", census)
    monkeypatch.setattr(module.processes.os, "killpg", killpg)
    monkeypatch.setattr(module.processes, "wait_for", wait_for)
    monkeypatch.setattr(module.processes.time, "sleep", lambda delay: None)
    if interrupted:
        with pytest.raises(RuntimeError, match="calibration interrupted"):
            module.adoption(tmp_path / "adoption")
    else:
        assert module.adoption(tmp_path / "adoption")["passed"] is True
    assert state["child_closed"] and state["reaped"]
    assert events.index("reaped-owner") < events.index("drained")
    assert events.index("close-child") < events.index("drained")


@pytest.mark.parametrize("mode,stage", [("crash", stage) for stage in (
    "before_intake", "after_intake", "prepared", "trigger", "config", "bound", "native", "claimed", "effect",
    "receipt", "collected", "committed")] + [("late", phase) for phase in (
    "db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush")])
async def test_uncertain_seed_keeps_unspent_response_capacity(module, monkeypatch, tmp_path, mode, stage):
    async def base(root, name):
        root.mkdir()
        return {"start": 100, "sources": {}, "source_pin": "a" * 40}

    monkeypatch.setattr(module.fixture, "seed", base)
    root = tmp_path / stage
    backend = SimpleNamespace(server=SimpleNamespace(server_port=12345))
    spec = await module.seed(root, stage, mode, backend)
    count = 1 if mode == "crash" and stage in {"before_intake", "after_intake", "committed"} else 2
    assert len(spec["responses"]) == count
    assert json.loads((root / "manifest.json").read_text())["responses"] == spec["responses"]
