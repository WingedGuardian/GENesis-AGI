"""``segment: incremental`` (#4813): the builder judges only what follows the marker.

Drives the real ``run_extraction_cycle`` and the real rebuild drain on an
in-memory database, faking only the transcript parse, the struggle score and the
builder call (as test_lesson_builder_cap and test_lesson_segment_shadow do).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import genesis.learning.procedural.judge as jm
import genesis.learning.procedural.lesson_builder_config as lbc
import genesis.learning.procedural.struggle_detector as sd
from genesis.db.crud import cc_sessions as sessions_crud
from genesis.db.crud import deferred_work as dw_crud
from genesis.db.schema import create_all_tables
from genesis.memory import extraction_job as ej

CC = "inc-session"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(tmp_path / "out"))


def _tool(n, *, result=True, args=None):
    return {
        "turn": n,
        "type": "tool",
        "tool": "Bash",
        "args_summary": args or f"cmd {n}",
        "outcome": "ok" if result else "error",
        "error_text": "",
        "tool_use_id": f"t{n}",
        "has_result": result,
    }


class _Builder:
    """Records each call's part (turns), context, origin and cap; stores nothing."""

    def __init__(self, result=None, raises=None):
        self.calls, self.result, self.raises = [], result or [], raises

    async def __call__(
        self,
        db,
        spine,
        haystack,
        score,
        router,
        *,
        source_session_id,
        max_new,
        shadow=False,
        context_text=None,
        origin_class=None,
    ):
        self.calls.append(
            {
                "turns": [e["turn"] for e in spine],
                "context": context_text,
                "origin": origin_class,
                "max_new": max_new,
                "shadow": shadow,
            }
        )
        if self.raises:
            raise self.raises
        return list(self.result)


def _setup(monkeypatch, *, spine, mode="live", segment="incremental", score=0.9, builder=None):
    monkeypatch.setattr(lbc, "load_config", lambda: {"mode": mode, "segment": segment})
    monkeypatch.setattr(sd, "build_spine_and_haystack", lambda p: ([dict(e) for e in spine], "hay"))
    monkeypatch.setattr(sd, "score_struggle", score if callable(score) else (lambda s: score))
    builder = builder or _Builder()
    monkeypatch.setattr(jm, "judge_multi_procedure", builder)
    return builder


async def _seed(db, tmp_path):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, cc_session_id, source_tag,"
        " started_at, last_activity_at, status, last_extracted_line)"
        " VALUES ('s1', 'foreground', 'm', ?, 'foreground', ?, ?, 'active', 0)",
        (CC, now, now),
    )
    await db.commit()
    _append(tmp_path)


def _append(tmp_path, n=2):
    with (tmp_path / f"{CC}.jsonl").open("a") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": uuid.uuid4().hex,
                        "timestamp": "2026-11-01T12:00:00Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": f"step {i}"}],
                        },
                    }
                )
                + "\n"
            )


async def _cycle(db, tmp_path):
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=MagicMock(
            success=True,
            content='{"extractions": []}',
            call_site_id="x",
            error=None,
        )
    )
    return await ej.run_extraction_cycle(
        db=db, store=AsyncMock(), router=router, transcript_dir=tmp_path
    )


async def _drain(db, tmp_path):
    await ej._drain_procedure_rebuilds(
        db=db,
        router=MagicMock(),
        transcript_dir=tmp_path,
        summary={},
        max_procedures_per_session=7,
    )


async def _marker(db):
    return await sessions_crud.get_lesson_built_through(db, "s1")


async def _items(db, status=None):
    sql = "SELECT * FROM deferred_work_queue WHERE work_type='procedure_rebuild'"
    rows = [dict(r) for r in await (await db.execute(sql)).fetchall()]
    return [r for r in rows if status is None or r["status"] == status]


SPINE = [_tool(1), _tool(2), _tool(3)]


# ── Stream 1 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_new_part_is_judged_and_the_marker_moves(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]]
    assert builder.calls[0]["shadow"] is False and builder.calls[0]["max_new"] == 7
    assert await _marker(db) == "t3"

    _setup(monkeypatch, spine=[*SPINE, _tool(4), _tool(5)], builder=builder)
    _append(tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls[1]["turns"] == [4, 5]
    assert "[T=3]" in builder.calls[1]["context"] and "[T=4]" not in builder.calls[1]["context"]
    assert await _marker(db) == "t5"
    assert await _items(db) == []


@pytest.mark.asyncio
async def test_the_origin_comes_from_the_whole_spine(db, tmp_path, monkeypatch):
    """An external read earlier in the session still marks a later part."""
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    external = [_tool(1) | {"tool": "WebFetch"}, _tool(2), _tool(3), _tool(4)]
    _setup(monkeypatch, spine=external, builder=builder)
    _append(tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls[1]["turns"] == [4]
    assert builder.calls[1]["origin"] == sd.derive_session_origin(external)
    assert builder.calls[1]["origin"] != sd.derive_session_origin([_tool(4)])


@pytest.mark.asyncio
async def test_a_part_below_threshold_moves_the_marker_without_a_build(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE, score=0.0)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls == []
    assert await _marker(db) == "t3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [jm.ProcedureBuilderUnavailable("down"), TimeoutError(), RuntimeError("bug")],
)
async def test_a_failed_build_holds_the_marker_and_queues_one_continue(
    db,
    tmp_path,
    monkeypatch,
    error,
):
    _setup(monkeypatch, spine=SPINE, builder=_Builder(raises=error))
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    _append(tmp_path)
    await _cycle(db, tmp_path)
    assert await _marker(db) is None
    (item,) = await _items(db, "pending")
    assert json.loads(item["payload_json"]) == {
        "session_id": "s1",
        "cc_session_id": CC,
        "trigger": "score",
    }


@pytest.mark.asyncio
async def test_a_spent_cap_holds_and_queues_without_a_build(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    for _ in range(7):
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used,"
            " context_tags, created_at, source) VALUES (?, 't', 'p', '[]', '[]', '[]', ?, ?)",
            (uuid.uuid4().hex, datetime.now(UTC).isoformat(), json.dumps({"session_id": CC})),
        )
    await db.commit()
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls == [] and await _marker(db) is None
    assert len(await _items(db, "pending")) == 1


@pytest.mark.asyncio
async def test_a_long_backlog_is_judged_four_parts_per_run(db, tmp_path, monkeypatch):
    spine = [_tool(i, args="x" * 200) for i in range(1, 21)]
    per = len(sd.judge_line(spine[0])) + 1
    monkeypatch.setattr(sd, "_MAX_SPINE_CHARS", per * 3)
    builder = _setup(monkeypatch, spine=spine)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert len(builder.calls) == ej._PARTS_PER_RUN
    assert builder.calls[0]["turns"] == [1, 2, 3]
    judged = [t for c in builder.calls for t in c["turns"]]
    assert judged == list(range(1, len(judged) + 1))  # contiguous, none twice
    assert await _marker(db) == f"t{judged[-1]}"
    assert len(await _items(db, "pending")) == 1


@pytest.mark.asyncio
async def test_candidates_count_for_the_part_reaching_the_end(db, tmp_path, monkeypatch):
    """The chunk path flags the latest messages; with no struggle score the part
    that reaches the end is still built, and the queued item remembers why."""
    builder = _setup(monkeypatch, spine=SPINE, score=0.0)
    await _seed(db, tmp_path)
    marker_parts = await ej._judge_from_marker(
        db,
        "s1",
        CC,
        [dict(e) for e in SPINE],
        "hay",
        MagicMock(),
        marker=None,
        transcript_path=tmp_path / f"{CC}.jsonl",
        candidates=True,
        max_procedures_per_session=7,
        summary={},
    )
    assert marker_parts == "done" and [c["turns"] for c in builder.calls] == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_incremental_under_shadow_mode_runs_as_shadow(db, tmp_path, monkeypatch, caplog):
    builder = _setup(monkeypatch, spine=SPINE, mode="shadow")
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]] and builder.calls[0]["shadow"]
    assert builder.calls[0]["context"] is None  # the whole-session shadow build
    assert any("Lesson segment [shadow]" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_refused_move_queues_nothing(db, tmp_path, monkeypatch):
    _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    monkeypatch.setattr(
        sessions_crud, "advance_lesson_built_through", AsyncMock(return_value=False)
    )
    await _cycle(db, tmp_path)
    assert await _items(db) == []


# ── the drain ───────────────────────────────────────────────────────────────


async def _queue(db, *, trigger="score", session="s1"):
    await ej._enqueue_procedure_rebuild(db, session, CC, trigger=trigger)


@pytest.mark.asyncio
async def test_the_drain_continues_from_the_marker_and_completes(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=[*SPINE, _tool(4)])
    await _seed(db, tmp_path)
    await sessions_crud.advance_lesson_built_through(db, "s1", expected=None, new="t2")
    await _queue(db)
    await _drain(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[3, 4]]
    assert await _marker(db) == "t4"
    (item,) = await _items(db)
    assert item["status"] == "completed" and item["attempts"] == 1


@pytest.mark.asyncio
async def test_a_legacy_item_under_incremental_judges_from_the_marker(db, tmp_path, monkeypatch):
    """One item kind, read by the current mode: no whole-session re-judge."""
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await sessions_crud.advance_lesson_built_through(db, "s1", expected=None, new="t2")
    queue = __import__("genesis.resilience.deferred_work", fromlist=["x"]).DeferredWorkQueue(db)
    await queue.enqueue(
        work_type="procedure_rebuild",
        call_site_id="38_procedure_extraction",
        priority=40,
        payload=json.dumps({"session_id": "s1", "cc_session_id": CC}),
        reason="legacy",
        staleness_policy="drain",
    )
    await _drain(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[3]]


@pytest.mark.asyncio
async def test_the_drain_honours_a_candidates_trigger(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE, score=0.0)
    await _seed(db, tmp_path)
    await _queue(db, trigger="candidates")
    await _drain(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_a_drain_with_entries_left_queues_a_fresh_item(db, tmp_path, monkeypatch):
    spine = [_tool(i, args="x" * 200) for i in range(1, 21)]
    per = len(sd.judge_line(spine[0])) + 1
    monkeypatch.setattr(sd, "_MAX_SPINE_CHARS", per * 3)
    _setup(monkeypatch, spine=spine)
    await _seed(db, tmp_path)
    await _queue(db)
    await _drain(db, tmp_path)
    assert [i["status"] for i in await _items(db)] == ["completed", "pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [jm.ProcedureBuilderUnavailable("down"), TimeoutError(), RuntimeError("bug")],
)
async def test_a_drain_failure_keeps_the_item_pending_with_its_attempt(
    db,
    tmp_path,
    monkeypatch,
    error,
):
    _setup(monkeypatch, spine=SPINE, builder=_Builder(raises=error))
    await _seed(db, tmp_path)
    await _queue(db)
    await _drain(db, tmp_path)
    (item,) = await _items(db)
    assert item["status"] == "pending" and item["attempts"] == 1


@pytest.mark.asyncio
async def test_eight_failed_attempts_end_in_the_observation(db, tmp_path, monkeypatch):
    _setup(monkeypatch, spine=SPINE, builder=_Builder(raises=RuntimeError("bug")))
    await _seed(db, tmp_path)
    await _queue(db)
    for _ in range(ej._MAX_PROCEDURE_REBUILD_ATTEMPTS + 1):
        await _drain(db, tmp_path)
    (item,) = await _items(db)
    assert item["status"] == "discarded"
    row = await (
        await db.execute("SELECT content FROM observations WHERE type='procedure_extraction_lost'")
    ).fetchone()
    assert row and "provider-exhausted" not in row[0] and "failed" in row[0]


@pytest.mark.asyncio
async def test_an_item_for_a_deleted_session_row_is_discarded(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await _queue(db)
    await db.execute("DELETE FROM cc_sessions WHERE id='s1'")
    await db.commit()
    await _drain(db, tmp_path)
    assert builder.calls == []
    (item,) = await _items(db)
    assert item["status"] == "discarded"


@pytest.mark.asyncio
async def test_an_item_another_process_claimed_is_skipped(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await _queue(db)
    real_claim = dw_crud.claim_pending

    async def claimed_elsewhere(db_, id_, **kw):
        await real_claim(db_, id_, **kw)  # the other process wins
        return await real_claim(db_, id_, **kw)

    monkeypatch.setattr(dw_crud, "claim_pending", claimed_elsewhere)
    await _drain(db, tmp_path)
    assert builder.calls == []


@pytest.mark.asyncio
async def test_the_drain_finds_a_voice_transcript(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    voice = tmp_path / "voice"
    voice.mkdir()
    (tmp_path / f"{CC}.jsonl").rename(voice / f"{CC}.jsonl")
    monkeypatch.setattr(ej, "voice_transcript_dir", lambda: voice)
    await _queue(db)
    await _drain(db, tmp_path / "nowhere")
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_whole_mode_keeps_todays_rebuild(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE, segment="whole")
    await _seed(db, tmp_path)
    await sessions_crud.advance_lesson_built_through(db, "s1", expected=None, new="t2")
    await _queue(db)
    await _drain(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]] and builder.calls[0][
        "context"
    ] is None


@pytest.mark.asyncio
async def test_claim_pending_takes_only_a_pending_item(db):
    await _queue(db)
    (item,) = await _items(db)
    assert await dw_crud.claim_pending(db, item["id"], last_attempt_at="now") is True
    assert await dw_crud.claim_pending(db, item["id"], last_attempt_at="now") is False
    (after,) = await _items(db)
    assert (after["status"], after["attempts"]) == ("processing", 1)


# ── the judge ───────────────────────────────────────────────────────────────


def test_the_prompt_shows_earlier_turns_for_reference_only():
    prompt = jm._build_multi_struggle_prompt(
        "[T=4] TOOL: Bash x -> OK", 0.5, "[T=3] TOOL: Bash y -> OK"
    )
    assert jm._CONTEXT_HEADING in prompt and jm._CONTEXT_RULE in prompt
    assert prompt.index("[T=3]") < prompt.index("## Action Spine") < prompt.index("[T=4]")
    assert jm._CONTEXT_HEADING not in jm._build_multi_struggle_prompt("[T=4]", 0.5)


@pytest.mark.asyncio
async def test_the_judge_uses_the_passed_origin_and_logs_part_grounding(db, monkeypatch, caplog):
    seen = {}

    async def fake_store(db_, data, router, **kw):
        seen.update(kw)
        return "id1"

    monkeypatch.setattr(
        jm,
        "_call_and_parse_list",
        AsyncMock(
            return_value=[
                {"task_type": "t", "principle": "p", "steps": ["run cmd 4"]},
            ]
        ),
    )
    monkeypatch.setattr(jm, "_store_judged_procedure", fake_store)
    with caplog.at_level(logging.INFO, logger="genesis.learning.procedural.judge"):
        out = await jm.judge_multi_procedure(
            db,
            [_tool(4)],
            "hay cmd 4",
            0.5,
            MagicMock(),
            source_session_id=CC,
            context_text="[T=3] TOOL: Bash y -> OK",
            origin_class="external_untrusted",
        )
    assert out == ["id1"] and seen["origin_class"] == "external_untrusted"
    assert any("grounding [part]" in r.getMessage() for r in caplog.records)


# ── review round (SF-1, SF-3, N-1) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_part_that_times_out_holds_the_marker_and_queues(db, tmp_path, monkeypatch):
    """The timeout wraps each part's builder call, so a slow part fails alone."""
    import asyncio

    class Slow(_Builder):
        async def __call__(self, *a, **k):
            await super().__call__(*a, **k)
            await asyncio.sleep(5)
            return []

    _setup(monkeypatch, spine=SPINE, builder=Slow())
    monkeypatch.setattr(jm, "JUDGE_TIMEOUT_SECS", 0.05)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert await _marker(db) is None
    assert len(await _items(db, "pending")) == 1


@pytest.mark.asyncio
async def test_a_partial_parse_hold_is_observed_once(db, tmp_path, monkeypatch):
    _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await sessions_crud.advance_lesson_built_through(db, "s1", expected=None, new="toolu_later")
    with (tmp_path / f"{CC}.jsonl").open("a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_later", "name": "Bash", "input": {}}]}}) + "\n")
    await _cycle(db, tmp_path)
    _append(tmp_path)
    await _cycle(db, tmp_path)
    assert await _marker(db) == "toolu_later"
    row = await (await db.execute(
        "SELECT COUNT(*) FROM observations WHERE type='lesson_builder_parse_stopped_short'"
    )).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_candidates_trigger_only_the_part_that_reaches_the_end(db, tmp_path, monkeypatch):
    spine = [_tool(i, args="x" * 200) for i in range(1, 7)]
    per = len(sd.judge_line(spine[0])) + 1
    monkeypatch.setattr(sd, "_MAX_SPINE_CHARS", per * 3)
    builder = _setup(monkeypatch, spine=spine, score=0.0)
    await _seed(db, tmp_path)
    out = await ej._judge_from_marker(
        db, "s1", CC, [dict(e) for e in spine], "hay", MagicMock(), marker=None,
        transcript_path=tmp_path / f"{CC}.jsonl", candidates=True,
        max_procedures_per_session=7, summary={},
    )
    assert out == "done"
    assert [c["turns"] for c in builder.calls] == [[6]]  # parts 1-3 below threshold, part 4 reaches the end


@pytest.mark.asyncio
async def test_an_incremental_reset_judges_from_the_start_once(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    await _seed(db, tmp_path)
    await sessions_crud.advance_lesson_built_through(db, "s1", expected=None, new="toolu_gone")
    await _cycle(db, tmp_path)
    assert [c["turns"] for c in builder.calls] == [[1, 2, 3]]
    assert await _marker(db) == "t3"
    _append(tmp_path)
    await _cycle(db, tmp_path)
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_a_candidates_request_upgrades_a_pending_score_item(db):
    """The drain cannot re-run the chunk path, so a later candidates trigger
    must not be dropped behind an earlier score item for the same session."""
    await ej._enqueue_procedure_rebuild(db, "s1", CC, trigger="score", dedup=True)
    await ej._enqueue_procedure_rebuild(db, "s1", CC, trigger="candidates", dedup=True)
    await ej._enqueue_procedure_rebuild(db, "s1", CC, trigger="score", dedup=True)
    (item,) = await _items(db)
    assert json.loads(item["payload_json"])["trigger"] == "candidates"


@pytest.mark.asyncio
async def test_a_failed_marker_read_queues_a_continue(db, tmp_path, monkeypatch):
    builder = _setup(monkeypatch, spine=SPINE)
    monkeypatch.setattr(
        sessions_crud, "get_lesson_built_through", AsyncMock(side_effect=RuntimeError("x")),
    )
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls == []
    assert len(await _items(db, "pending")) == 1
