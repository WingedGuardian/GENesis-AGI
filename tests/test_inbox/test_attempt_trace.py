"""Controlled inbox scan → invoker/retry → persisted trace and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
from genesis.cc import invoker as invoker_mod
from genesis.cc import transient_retry
from genesis.cc.invoker import CCInvoker
from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCInvocation, CCOutput
from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.types import InboxConfig
from genesis.inbox.writer import ResponseWriter
from genesis.observability import span_reader, spans
from genesis.observability.session_context import session_scope
from genesis.observability.span_writer import SpanWriter
from genesis.util.execution_privacy import private_execution

URL = "https://example.invalid/library"
RESPONSE = (
    "# Inbox Evaluation\n\n## Library\n**Source:** "
    + URL
    + "\n"
    + "Synthetic evidence for a small adoption experiment. " * 6
    + "\n### Recommendation\n```yaml\naction: ADAPT\nnext_step: Test the adapter\n"
    "effort: Small\nconfidence: medium\n```\n"
)


@pytest.fixture
def transport(monkeypatch, tmp_path):
    state = SimpleNamespace(mode="success", calls=[], sleeps=[])
    monkeypatch.setattr(invoker_mod.oom, "_MEMBERSHIP", tmp_path / "absent-cgroup-membership")

    async def spawn(*args, **kwargs):
        n = len(state.calls) + 1
        state.calls.append(n)
        sid = f"synthetic-cli-{1 if state.mode == 'retry_same_id' else n}"
        event = {
            "type": "result",
            "is_error": False,
            "session_id": sid,
            "result": RESPONSE,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "total_cost_usd": 0,
            "duration_ms": 1,
        }
        if state.mode in ("retry", "retry_same_id", "cancel") and n == 1 or state.mode == "hold":
            event.update(
                is_error=True,
                api_error_status=529,
                result="529 overloaded",
                num_turns=2 if state.mode == "hold" else 1,
            )
        elif state.mode == "error":
            event.update(is_error=True, result="ordinary synthetic failure")
        return SimpleNamespace(
            pid=900000000 + n,
            returncode=0,
            communicate=AsyncMock(return_value=(json.dumps(event).encode(), b"")),
        )

    async def sleep(delay):
        state.sleeps.append(delay)
        if state.mode == "cancel":
            raise asyncio.CancelledError()

    monkeypatch.setattr(invoker_mod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(invoker_mod, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(invoker_mod, "_settings_env_pins", lambda *a: {})
    monkeypatch.setattr(invoker_mod, "set_oom_score_adj", lambda *a: None)
    monkeypatch.setattr(invoker_mod, "_emit_invocation_failed_event", AsyncMock())
    monkeypatch.setattr(invoker_mod.roster, "apply_active", lambda i: (i, "claude"))
    monkeypatch.setattr(transient_retry, "_sleep", sleep)
    return state


def controlled_invoker(monkeypatch):
    inv = CCInvoker(claude_path="/no-cli-is-run")

    async def cost(output):
        return output

    for name, value in {
        "_network_preflight": AsyncMock(),
        "_build_args": lambda *a, **k: ["/no-cli-is-run"],
        "verify_allowlist_enforceable": AsyncMock(),
        "_build_env": lambda *a: {},
        "_apply_login_fallback": AsyncMock(return_value={}),
        "_launch_env": lambda *a: {},
        "_with_cost_semantics": cost,
        "_notify_status_change": AsyncMock(),
        "_fire_downgrade_callback": AsyncMock(),
        "_fire_empty_output_callback": AsyncMock(),
    }.items():
        monkeypatch.setattr(inv, name, value)
    return inv


def monitor(db, folder, inv):
    dispatcher = SimpleNamespace(
        route=AsyncMock(
            return_value=AutonomousDispatchDecision(mode="cli", reason="fixture approval")
        ),
        approval_gate=SimpleNamespace(
            find_site_pending=AsyncMock(return_value=None),
            approval_manager=SimpleNamespace(
                get_by_id=AsyncMock(return_value=None), cancel=AsyncMock()
            ),
            mark_consumed=AsyncMock(return_value=True),
        ),
    )
    mon = InboxMonitor(
        db=db,
        invoker=inv,
        session_manager=SessionManager(db=db, invoker=inv),
        config=InboxConfig(watch_path=folder, evaluation_cooldown_seconds=0),
        writer=ResponseWriter(watch_path=folder, timezone="UTC"),
        clock=lambda: datetime(2026, 11, 8, tzinfo=UTC),
    )
    mon.set_autonomous_dispatcher(dispatcher)
    return mon, dispatcher


@pytest.fixture
async def trace_db(monkeypatch, tmp_path):
    path = tmp_path / "state.db"
    monkeypatch.setenv("GENESIS_DB_PATH", str(path))
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        writer = SpanWriter()
        writer.set_db(db, process="test")
        # Real serialization/persistence, with deterministic owned flushes.
        monkeypatch.setattr(writer, "_schedule_flush", lambda: None)
        monkeypatch.setattr(spans, "_writer", writer)
        monkeypatch.setattr(spans, "_enabled", True)
        yield db, writer


@pytest.mark.parametrize("mode", ["success", "retry", "retry_same_id", "error", "hold", "cancel"])
@pytest.mark.parametrize("capture", [True, False])
async def test_scan_invoker_delivery_and_attempt_trace(
    trace_db, tmp_path, monkeypatch, transport, mode, capture
):
    db, writer = trace_db
    transport.mode = mode
    monkeypatch.setattr(spans, "_enabled", capture)
    folder = tmp_path / "inbox"
    folder.mkdir()
    source = folder / "Genesis.md"
    source.write_text(URL + "\n")
    inv = controlled_invoker(monkeypatch)
    mon, dispatcher = monitor(db, folder, inv)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await mon.check_once()
    else:
        await mon.check_once()
    assert dispatcher.route.await_count == 1
    row = await inbox_items.get_by_file_path(db, str(source))
    assert row is not None and row["batch_id"]
    success = mode in ("success", "retry", "retry_same_id")
    retried = mode in ("retry", "retry_same_id")
    assert (row["status"] == "completed") == success
    baseline = await inbox_items.get_evaluated_content(db, str(source))
    assert (URL in (baseline or "")) == success
    files = list(folder.glob("*.genesis.md"))
    assert len(files) == int(success)
    if success:
        assert "Test the adapter" in files[0].read_text()
        followups = await (
            await db.execute("SELECT domain,source_session FROM follow_ups")
        ).fetchall()
        assert len(followups) == 1
        assert followups[0]["domain"] == "internal"
        final_id = 1 if mode == "retry_same_id" else len(transport.calls)
        assert followups[0]["source_session"] == f"synthetic-cli-{final_id}"
    if mode == "hold":
        assert row["error_message"].startswith(inbox_items.REPLAY_UNSAFE_PREFIX)
    assert transport.calls == ([1, 2] if retried else [1])
    assert transport.sleeps == ([30] if retried or mode == "cancel" else [])
    await writer._flush_to_db()
    raw = [dict(r) for r in await (await db.execute("SELECT * FROM otel_spans")).fetchall()]
    if not capture:
        assert raw == []
    else:
        parents = [r for r in raw if r["name"] == "inbox.evaluate"]
        assert len(parents) == 1
        parent = parents[0]
        assert json.loads(parent["attributes_json"]) == {
            "inbox_batch_id": row["batch_id"],
            "inbox_item_id": row["id"],
            "evaluation_outcome": "returned" if success else "not_returned",
        }
        assert parent["status"] == ("ok" if success else "error")
        if not success:
            assert "synthetic" not in parent["status_message"]
        trace = await span_reader.get_trace(db, parent["trace_id"])
        children = [r for r in span_reader.flatten_tree(trace) if r["name"] == "cc.session"]
        assert len(children) == len(transport.calls)
        assert len({r["span_id"] for r in children}) == len(children)
        assert all(r["parent_span_id"] == parent["span_id"] for r in children)
        assert [r["attributes"]["cc_session_id"] for r in children] == [
            f"synthetic-cli-{1 if mode == 'retry_same_id' else n}" for n in transport.calls
        ]
    if success or mode == "hold":
        await mon.check_once()
        assert transport.calls == ([1, 2] if retried else [1])


async def test_concurrent_batch_parents_do_not_cross(trace_db, tmp_path, monkeypatch, transport):
    traces, writer = trace_db
    async with contextlib.AsyncExitStack() as stack:
        monitors = []
        expected = {}
        for n in range(2):
            db = await stack.enter_async_context(aiosqlite.connect(tmp_path / f"business-{n}.db"))
            db.row_factory = aiosqlite.Row
            await create_all_tables(db)
            folder = tmp_path / str(n)
            folder.mkdir()
            (folder / "Genesis.md").write_text(URL + "\n")
            mon, _ = monitor(db, folder, controlled_invoker(monkeypatch))
            monitors.append(mon)
            expected[n] = db
        await asyncio.gather(*(mon.check_once() for mon in monitors))
        await writer._flush_to_db()
        rows = [
            dict(r) for r in await (await traces.execute("SELECT * FROM otel_spans")).fetchall()
        ]
        parents = [r for r in rows if r["name"] == "inbox.evaluate"]
        assert len(parents) == 2 and len({r["span_id"] for r in parents}) == 2
        batches = set()
        for db in expected.values():
            row = await (await db.execute("SELECT id,batch_id,status FROM inbox_items")).fetchone()
            assert row["status"] == "completed"
            batches.add((row["id"], row["batch_id"]))
        assert {
            (
                json.loads(p["attributes_json"])["inbox_item_id"],
                json.loads(p["attributes_json"])["inbox_batch_id"],
            )
            for p in parents
        } == batches
        for parent in parents:
            trace = await span_reader.get_trace(traces, parent["trace_id"])
            children = [r for r in span_reader.flatten_tree(trace) if r["name"] == "cc.session"]
            assert len(children) == 1 and children[0]["parent_span_id"] == parent["span_id"]
        assert spans.current_trace_context() is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("private", [False, True])
async def test_persisted_identity_through_existing_http_api(
    trace_db, monkeypatch, streaming, private
):
    from flask import Flask

    from genesis.dashboard.api import blueprint
    from genesis.runtime import GenesisRuntime

    db, writer = trace_db
    inv = controlled_invoker(monkeypatch)
    output = CCOutput(
        session_id="synthetic-private-cli" if private else "synthetic-public-cli",
        text="synthetic output",
        model_used="sonnet",
        cost_usd=0,
        input_tokens=0,
        output_tokens=0,
        duration_ms=1,
        exit_code=0,
    )
    monkeypatch.setattr(
        inv, "_run_streaming_inner" if streaming else "_run_inner", AsyncMock(return_value=output)
    )
    method = inv._run_streaming_traced if streaming else inv._run_traced
    with (
        session_scope("synthetic-manager-id"),
        private_execution() if private else contextlib.nullcontext(),
        spans.start_span("fixture.parent") as parent,
    ):
        result = await method(CCInvocation(prompt="synthetic"), "fixture")
    assert result.session_id == output.session_id
    await writer._flush_to_db()
    monkeypatch.setattr(
        GenesisRuntime,
        "instance",
        staticmethod(lambda: SimpleNamespace(is_bootstrapped=True, db=db)),
    )
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config.update(TESTING=True, GENESIS_EVENT_LOOP=asyncio.get_running_loop())

    def fetch():
        with app.test_client() as client:
            response = client.get(f"/api/genesis/spans/trace/{parent.trace_id}")
            assert response.status_code == 200
            return response.get_json()

    data = await asyncio.to_thread(fetch)
    child = next(row for row in data["spans"] if row["name"] == "cc.session")
    assert child["session_id"] == "synthetic-manager-id"
    assert child["parent_span_id"] == parent.span_id and child["depth"] == 1
    attrs = {key: value for key, value in child["attributes"].items() if key.startswith("cc_")}
    if private:
        assert attrs == {}
        assert output.session_id not in json.dumps(data)
    else:
        assert attrs == {
            "cc_session_id": output.session_id,
            "cc_identity_source": "cc_output",
            "cc_outcome": "returned",
        }


@pytest.mark.parametrize("fault", ["record", "flush", "parent", "child", "preflight"])
async def test_incomplete_trace_does_not_change_inbox_outcome(
    trace_db, tmp_path, monkeypatch, transport, fault
):
    from genesis.cc.exceptions import CCNetworkOfflineError

    db, writer = trace_db
    record = writer.record

    def maybe_record(span):
        if fault == "record":
            raise RuntimeError("synthetic record failure")
        if fault == "parent" and span.name == "inbox.evaluate":
            return
        if fault == "child" and span.name == "cc.session":
            return
        record(span)

    monkeypatch.setattr(writer, "record", maybe_record)
    if fault == "flush":
        monkeypatch.setattr(
            writer,
            "_db",
            SimpleNamespace(executemany=AsyncMock(side_effect=RuntimeError("synthetic flush"))),
        )
    folder = tmp_path / "inbox"
    folder.mkdir()
    source = folder / "Genesis.md"
    source.write_text(URL + "\n")
    inv = controlled_invoker(monkeypatch)
    if fault == "preflight":
        monkeypatch.setattr(
            inv, "_network_preflight", AsyncMock(side_effect=CCNetworkOfflineError("offline"))
        )
    mon, _ = monitor(db, folder, inv)
    await mon.check_once()
    row = await inbox_items.get_by_file_path(db, str(source))
    assert (row["status"] == "completed") == (fault != "preflight")
    assert len(list(folder.glob("*.genesis.md"))) == int(fault != "preflight")
    assert transport.calls == ([] if fault == "preflight" else [1])
    await writer._flush_to_db()
    rows = [dict(r) for r in await (await db.execute("SELECT * FROM otel_spans")).fetchall()]
    if fault in ("record", "flush"):
        assert rows == []
    else:
        assert len(rows) == 1
        trace = await span_reader.get_trace(db, rows[0]["trace_id"])
        assert trace["span_count"] == 1
        if fault == "parent":
            orphan = trace["roots"][0]
            assert orphan["name"] == "cc.session" and orphan["parent_span_id"]
            assert "inbox_batch_id" not in orphan["attributes"]
        else:
            assert rows[0]["name"] == "inbox.evaluate"
            attrs = json.loads(rows[0]["attributes_json"])
            assert attrs["evaluation_outcome"] == (
                "not_returned" if fault == "preflight" else "returned"
            )
    if fault != "preflight":
        await mon.check_once()
        assert transport.calls == [1]
