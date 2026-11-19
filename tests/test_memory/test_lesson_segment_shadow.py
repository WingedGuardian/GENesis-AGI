"""``segment: shadow`` in the extraction cycle (#4807).

Drives the real ``run_extraction_cycle`` builder path on an in-memory database,
faking only the transcript parse, the struggle score and the builder call (as
test_lesson_builder_cap does). The shadow step must change nothing the builder
does, log one line per built session, and move the progress marker only as the
incremental rule would.
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
from genesis.db.schema import create_all_tables
from genesis.memory import extraction_job as ej

CC = "seg-session"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def out_dir(tmp_path, monkeypatch):
    d = tmp_path / "out"
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(d))
    return d


def _tool(n, *, result=True):
    return {
        "turn": n,
        "type": "tool",
        "tool": "Bash",
        "args_summary": f"cmd {n}",
        "outcome": "ok" if result else "error",
        "error_text": "",
        "tool_use_id": f"t{n}",
        "has_result": result,
    }


SPINE = [_tool(1), _tool(2), _tool(3), _tool(4, result=False)]


class _Builder:
    def __init__(self):
        self.calls = []

    async def __call__(
        self, db, spine, haystack, score, router, *, source_session_id, max_new, shadow=False
    ):
        self.calls.append((len(spine), max_new, shadow))
        return []


def _setup(monkeypatch, *, mode="live", segment="shadow", builder=None, score=0.9, spine=SPINE):
    monkeypatch.setattr(lbc, "load_config", lambda: {"mode": mode, "segment": segment})
    monkeypatch.setattr(sd, "build_spine_and_haystack", lambda p: ([dict(e) for e in spine], "hay"))
    monkeypatch.setattr(sd, "score_struggle", lambda s: score)
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


async def _marker(db):
    return await sessions_crud.get_lesson_built_through(db, "s1")


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "Lesson segment [shadow]" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "shadow"])
async def test_shadow_logs_one_line_moves_the_marker_and_leaves_the_build_alone(
    db,
    tmp_path,
    monkeypatch,
    caplog,
    mode,
):
    builder = _setup(monkeypatch, mode=mode)
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert builder.calls == [(4, 7, mode == "shadow")]  # the whole spine, as before
    lines = _lines(caplog)
    assert len(lines) == 1
    assert "entries=3" in lines[0] and "pending_tail=1" in lines[0] and "marker=moved" in lines[0]
    assert "path=cycle" in lines[0]
    assert await _marker(db) == "t3"  # the in-flight t4 waits


@pytest.mark.asyncio
async def test_the_next_cycle_takes_only_what_follows_the_marker(db, tmp_path, monkeypatch, caplog):
    _setup(monkeypatch)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    _setup(monkeypatch, spine=[_tool(1), _tool(2), _tool(3), _tool(4), _tool(5)])
    _append(tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "entries=2" in line and "context=3" in line
    assert await _marker(db) == "t5"


@pytest.mark.asyncio
async def test_off_holds_the_marker(db, tmp_path, monkeypatch, caplog):
    builder = _setup(monkeypatch, mode="off")
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert builder.calls == []
    assert "marker=held:off" in _lines(caplog)[0]
    assert await _marker(db) is None


@pytest.mark.asyncio
async def test_a_failed_build_holds_the_marker(db, tmp_path, monkeypatch, caplog):
    _setup(monkeypatch, builder=AsyncMock(side_effect=jm.ProcedureBuilderUnavailable("down")))
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert _lines(caplog) == []
    assert await _marker(db) is None


@pytest.mark.asyncio
async def test_a_spent_cap_holds_the_marker_when_the_part_would_build(
    db, tmp_path, monkeypatch, caplog
):
    _setup(monkeypatch)
    for _ in range(7):
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used,"
            " context_tags, created_at, source) VALUES (?, 't', 'p', '[]', '[]', '[]', ?, ?)",
            (uuid.uuid4().hex, datetime.now(UTC).isoformat(), json.dumps({"session_id": CC})),
        )
    await db.commit()
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert "marker=held:cap" in _lines(caplog)[0]
    assert await _marker(db) is None


@pytest.mark.asyncio
async def test_a_part_below_threshold_moves_the_marker(db, tmp_path, monkeypatch, caplog):
    """Below threshold is a judgement too: nothing to build, so the part is done."""
    builder = _setup(monkeypatch, score=0.0)
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert builder.calls == []
    assert "would_trigger=False" in _lines(caplog)[0]
    assert await _marker(db) == "t3"


@pytest.mark.asyncio
async def test_whole_writes_no_line_and_no_marker(db, tmp_path, monkeypatch, caplog):
    builder = _setup(monkeypatch, segment="whole")
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert builder.calls == [(4, 7, False)]
    assert _lines(caplog) == []
    assert await _marker(db) is None


@pytest.mark.asyncio
async def test_a_marker_moved_elsewhere_is_not_overwritten(db, tmp_path, monkeypatch, caplog):
    """Compare-and-set: another process moved the marker after this cycle read it."""
    _setup(monkeypatch)
    await _seed(db, tmp_path)
    real_get = sessions_crud.get_lesson_built_through

    async def stale_read(db_, id_):
        value = await real_get(db_, id_)
        await db_.execute("UPDATE cc_sessions SET lesson_built_through='t9' WHERE id=?", (id_,))
        await db_.commit()
        return value

    monkeypatch.setattr(sessions_crud, "get_lesson_built_through", stale_read)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    assert "marker=held:changed-elsewhere" in _lines(caplog)[0]
    assert await real_get(db, "s1") == "t9"


@pytest.mark.asyncio
async def test_a_failed_marker_read_skips_the_step_and_keeps_the_build(
    db,
    tmp_path,
    monkeypatch,
    caplog,
):
    builder = _setup(monkeypatch)
    monkeypatch.setattr(
        sessions_crud, "get_lesson_built_through", AsyncMock(side_effect=RuntimeError("x"))
    )
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        summary = await _cycle(db, tmp_path)
    assert builder.calls == [(4, 7, False)] and summary["sessions_processed"] == 1
    assert _lines(caplog) == []


async def _set_marker(db, value):
    await db.execute("UPDATE cc_sessions SET lesson_built_through=? WHERE id='s1'", (value,))
    await db.commit()


@pytest.mark.asyncio
async def test_a_reset_whose_id_is_still_in_the_file_holds(db, tmp_path, monkeypatch, caplog):
    """The parse stopped short (or an older parse is behind a newer write): the
    marker is not in this spine but is in the file, so it must not move back."""
    _setup(monkeypatch)
    await _seed(db, tmp_path)
    await _set_marker(db, "toolu_later")
    with (tmp_path / f"{CC}.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "toolu_later", "name": "Bash", "input": {}}
                        ]
                    },
                }
            )
            + "\n"
        )
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "reset=True" in line and "marker=held:partial-parse" in line
    assert await _marker(db) == "toolu_later"


@pytest.mark.asyncio
async def test_a_reset_after_the_file_was_replaced_starts_over(db, tmp_path, monkeypatch, caplog):
    _setup(monkeypatch)
    await _seed(db, tmp_path)
    await _set_marker(db, "toolu_gone")
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "reset=True" in line and "entries=3" in line and "marker=moved" in line
    assert await _marker(db) == "t3"


@pytest.mark.asyncio
async def test_a_deleted_and_readopted_row_starts_from_the_beginning(
    db,
    tmp_path,
    monkeypatch,
    caplog,
):
    _setup(monkeypatch)
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    assert await _marker(db) == "t3"
    await db.execute("DELETE FROM cc_sessions WHERE id='s1'")
    await db.commit()
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "reset=False" in line and "entries=3" in line and "context=0" in line


@pytest.mark.asyncio
async def test_the_line_carries_the_whole_build_result(db, tmp_path, monkeypatch, caplog):
    class Storing(_Builder):
        async def __call__(self, *a, **k):
            await super().__call__(*a, **k)
            return ["a", "b"]

    _setup(monkeypatch, builder=Storing())
    await _seed(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "whole_score=0.90" in line and "stored=2" in line and "context_chars=0" in line


@pytest.mark.asyncio
async def test_a_marker_written_during_the_parse_is_never_moved_back(
    db,
    tmp_path,
    monkeypatch,
    caplog,
):
    """Another cycle (a backfill run beside the server) moves the marker while
    this one is parsing. Read after the parse, that newer marker would be missing
    from this cycle's older spine, read as a reset, and moved back to t3; read
    before the parse, the compare-and-set expects the old value and refuses."""
    _setup(monkeypatch)
    await _seed(db, tmp_path)

    async def other_cycle_writes_after_the_parse(db_, spine, cc_session_id):
        # Runs right after this cycle's parse and before its build.
        await db_.execute("UPDATE cc_sessions SET lesson_built_through='toolu_newer' WHERE id='s1'")
        await db_.commit()

    monkeypatch.setattr(ej, "_record_tool_outcomes", other_cycle_writes_after_the_parse)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _cycle(db, tmp_path)
    (line,) = _lines(caplog)
    assert "marker=held:changed-elsewhere" in line and "reset=False" in line
    assert await _marker(db) == "toolu_newer"


@pytest.mark.asyncio
async def test_a_shadow_step_error_never_fails_the_build(db, tmp_path, monkeypatch, caplog):
    builder = _setup(monkeypatch)
    monkeypatch.setattr(
        sessions_crud, "advance_lesson_built_through", AsyncMock(side_effect=RuntimeError("x"))
    )
    await _seed(db, tmp_path)
    with caplog.at_level(logging.WARNING, logger="genesis.memory.extraction_job"):
        summary = await _cycle(db, tmp_path)
    assert builder.calls == [(4, 7, False)] and summary["sessions_processed"] == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("Lesson segment shadow failed" in m for m in messages)
    assert not any("Procedure builder failed" in m for m in messages)


async def _drain(db, tmp_path):
    await ej._drain_procedure_rebuilds(
        db=db,
        router=AsyncMock(),
        transcript_dir=tmp_path,
        summary={},
        max_procedures_per_session=7,
    )


@pytest.mark.asyncio
async def test_a_rebuild_that_succeeds_in_the_drain_moves_the_marker(
    db, tmp_path, monkeypatch, caplog
):
    _setup(monkeypatch, builder=AsyncMock(side_effect=jm.ProcedureBuilderUnavailable("down")))
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)  # the build fails: held, and a rebuild is queued
    assert await _marker(db) is None
    builder = _setup(monkeypatch)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _drain(db, tmp_path)
    assert len(builder.calls) == 1
    (line,) = _lines(caplog)
    assert "lever=live" in line and "path=drain" in line and "marker=moved" in line
    assert await _marker(db) == "t3"


@pytest.mark.asyncio
async def test_a_rebuild_that_fails_in_the_drain_holds_the_marker(
    db, tmp_path, monkeypatch, caplog
):
    _setup(monkeypatch, builder=AsyncMock(side_effect=jm.ProcedureBuilderUnavailable("down")))
    await _seed(db, tmp_path)
    await _cycle(db, tmp_path)
    with caplog.at_level(logging.INFO, logger="genesis.memory.extraction_job"):
        await _drain(db, tmp_path)
    assert _lines(caplog) == []
    assert await _marker(db) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("whole", "whole"),
        ("shadow", "shadow"),
        ("incremental", "incremental"),
        ("bad", "shadow"),
        (None, "shadow"),
    ],
)
def test_segment_mode_reads_and_degrades_to_shadow(monkeypatch, value, expected):
    monkeypatch.setattr(lbc, "load_config", lambda: {"mode": "live", "segment": value})
    assert lbc.segment_mode() == expected


def test_the_shipped_segment_is_shadow():
    assert lbc.load_config()["segment"] == "shadow"


def test_settings_domain_validates_segment():
    from genesis.mcp.health.settings import _validate_lesson_builder

    assert _validate_lesson_builder({"segment": "whole"}) == []
    assert _validate_lesson_builder({"segment": "partial"})
    assert _validate_lesson_builder({"mode": "live", "segment": "incremental"}) == []
