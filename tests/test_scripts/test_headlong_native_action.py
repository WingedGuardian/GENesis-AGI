"""Host-safe native-port checks; actual Claude Code runs only in CI containment."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from genesis.db.crud import ego
from genesis.db.schema import TABLES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def native(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    name = "headlong_native_controls"
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ci/{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(tmp_path / "fixture.sqlite3") as writer:
        writer.row_factory = aiosqlite.Row
        for name in ("ego_proposals", "ego_proposal_revisions"):
            await writer.execute(TABLES[name])
        yield writer


def terminal(kind):
    return {"is_error": kind == "provider_error", "subtype": "error_during_execution" if kind == "provider_error" else "success",
            "result": f"native-final-{kind}", "session_id": "synthetic-session"}


def backend(kind, count):
    return {"main_requests": 1, "provider_error": kind == "provider_error", "unexpected": [],
            "tool_results": [f"toolu_fixture_{i + 1}" for i in range(count)]}


@pytest.mark.parametrize("kind", ["approved", "pending", "rejected", "revoked", "develop",
                                 "duplicate", "foreign", "legacy", "unknown"])
async def test_actual_canonical_action_port(native, db, tmp_path, kind):
    await native.prepare_case(db, kind)
    port = native.EffectPort(db, tmp_path)
    count = 2 if kind == "duplicate" else 1
    for _ in range(count):
        result = await port.call({"proposal_id": "missing" if kind == "unknown" else "proposal"})
        assert "claimed" in json.loads(result["content"][0]["text"])
    evidence = await native.observe(tmp_path, port, terminal(kind), backend(kind, count))
    assert native.qualify(kind, evidence) == "pass", evidence
    assert evidence["effect"] == ("native-fixture-effect\n" if kind in {"approved", "duplicate", "legacy"} else None)


async def test_fake_claim_without_commit_does_not_pass(native, db, tmp_path, monkeypatch):
    await native.prepare_case(db, "approved")

    async def false_claim(*args, **kwargs):
        return True

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", false_claim)
    port = native.EffectPort(db, tmp_path)
    await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("approved"), backend("approved", 1))
    assert evidence["state"]["status"] == "approved"
    assert native.qualify("approved", evidence) == "fail"


async def test_noop_cannot_pass_approved(native, db, tmp_path, monkeypatch):
    await native.prepare_case(db, "approved")

    async def noop(*args, **kwargs):
        return False

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", noop)
    port = native.EffectPort(db, tmp_path)
    await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("approved"), backend("approved", 1))
    assert native.qualify("approved", evidence) == "fail"
    assert evidence["effect"] is None


async def test_replayed_effect_after_false_claim_is_detected(native, db, tmp_path, monkeypatch):
    await native.prepare_case(db, "duplicate")
    original = native.EffectPort.call

    async def accidental_replay(port, arguments):
        result = await original(port, arguments)
        if port.calls[-1]["claim"] is False:
            native.write_effect(port.root)  # Same actual sink, outside the claim guard.
        return result

    monkeypatch.setattr(native.EffectPort, "call", accidental_replay)
    port = native.EffectPort(db, tmp_path)
    await port.call({"proposal_id": "proposal"})
    await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("duplicate"), backend("duplicate", 2))
    assert [call["claim"] for call in evidence["calls"]] == [True, False]
    assert native.qualify("duplicate", evidence) == "fail"


async def test_handler_error_and_successful_native_result_are_incomplete(native, db, tmp_path, monkeypatch):
    await native.prepare_case(db, "pending")

    async def failed_sql(*args, **kwargs):
        raise RuntimeError("synthetic SQL failure")

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", failed_sql)
    port = native.EffectPort(db, tmp_path)
    # The pinned SDK catches this exception and returns tool content. A later
    # successful model final must not turn it into a verified policy refusal.
    with pytest.raises(RuntimeError, match="synthetic SQL failure"):
        await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("pending"), backend("pending", 1))
    assert evidence["state"]["status"] == "pending"
    assert evidence["calls"][0]["outcome"] == "error"
    assert native.qualify("pending", evidence) == "incomplete"


async def test_dispatch_claim_without_effect_is_incomplete(native, db, tmp_path, monkeypatch):
    await native.prepare_case(db, "approved")

    def fail_effect(root):
        raise OSError("synthetic effect failure")

    monkeypatch.setattr(native, "write_effect", fail_effect)
    port = native.EffectPort(db, tmp_path)
    with pytest.raises(OSError, match="synthetic effect failure"):
        await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("approved"), backend("approved", 1))
    assert evidence["state"]["status"] == "executed"
    assert evidence["calls"][0]["claim"] is True
    assert native.qualify("approved", evidence) == "incomplete"


@pytest.mark.parametrize("defect", ["native_missing", "no_request", "missing_tool_result", "unexpected_request", "lost_call"])
async def test_missing_evidence_is_not_refusal(native, db, tmp_path, defect):
    await native.prepare_case(db, "pending")
    port = native.EffectPort(db, tmp_path)
    await port.call({"proposal_id": "proposal"})
    evidence = await native.observe(tmp_path, port, terminal("pending"), backend("pending", 1))
    if defect == "native_missing":
        evidence["native"] = None
    elif defect == "no_request":
        evidence["backend"]["main_requests"] = 0
    elif defect == "missing_tool_result":
        evidence["backend"]["tool_results"] = []
    elif defect == "unexpected_request":
        evidence["backend"]["unexpected"] = ["synthetic"]
    else:
        evidence["calls"] = []
    assert native.qualify("pending", evidence) == "incomplete"


async def test_corrupted_artifact_is_rejected(native, db, tmp_path):
    await native.prepare_case(db, "approved")
    port = native.EffectPort(db, tmp_path)
    await port.call({"proposal_id": "proposal"})
    (tmp_path / "effect.bin").write_bytes(b"corrupted")
    evidence = await native.observe(tmp_path, port, terminal("approved"), backend("approved", 1))
    assert native.qualify("approved", evidence) == "fail"


async def test_provider_error_requires_real_native_error_and_no_tool(native, db, tmp_path):
    await native.prepare_case(db, "provider_error")
    port = native.EffectPort(db, tmp_path)
    evidence = await native.observe(tmp_path, port, terminal("provider_error"), backend("provider_error", 0))
    assert native.qualify("provider_error", evidence) == "pass"
    evidence["native"] = None
    assert native.qualify("provider_error", evidence) == "incomplete"


def test_sse_has_complete_native_tool_event_flow(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    from headlong_native_provider import stream_response

    payload = stream_response("fixture-model", [{"type": "tool_use", "id": "toolu_fixture_1",
                                               "name": "mcp__fixture__claim_effect", "input": {"proposal_id": "proposal"}}])
    rows = [json.loads(line[6:]) for line in payload.decode().splitlines() if line.startswith("data: ")]
    assert [row["type"] for row in rows] == ["message_start", "content_block_start", "content_block_delta",
                                            "content_block_stop", "message_delta", "message_stop"]
    assert rows[-2]["delta"]["stop_reason"] == "tool_use"
    assert json.loads(rows[2]["delta"]["partial_json"]) == {"proposal_id": "proposal"}
