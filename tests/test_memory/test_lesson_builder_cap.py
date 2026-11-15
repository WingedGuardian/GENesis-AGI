"""The whole-session lesson builder: per-session cap over a rolling 24h, and the
off/shadow/live lever.

The cap used to be a local counter that restarted at 0 every extraction cycle,
so a long session kept getting a fresh allowance and one reached 811 lessons.
These tests drive the real ``run_extraction_cycle`` builder path and the rebuild
drain against an in-memory database; only the transcript parse, the struggle
score and the builder call are faked.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import genesis.learning.procedural.judge as jm
import genesis.learning.procedural.lesson_builder_config as lbc
import genesis.learning.procedural.struggle_detector as sd
from genesis.db.crud import deferred_work as dw_crud
from genesis.db.schema import create_all_tables
from genesis.memory import extraction_job as ej

CC = "cap-session"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


_DEFAULT = object()


async def _add_lesson(db, *, session=CC, age=timedelta(0), source=_DEFAULT):
    created = (datetime.now(UTC) - age).isoformat()
    if source is _DEFAULT:
        source = json.dumps({"type": "struggle_extraction", "session_id": session})
    await db.execute(
        "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used,"
        " context_tags, created_at, source) VALUES (?, 't', 'p', '[]', '[]', '[]', ?, ?)",
        (uuid.uuid4().hex, created, source),
    )
    await db.commit()


class _Builder:
    """Fake builder: stores ``yield_n`` lessons (capped at ``max_new``) and
    records each call's ``max_new``."""

    def __init__(self, db, yield_n=7):
        self.db, self.yield_n, self.calls = db, yield_n, []

    async def __call__(
        self, db, spine, haystack, score, router, *, source_session_id, max_new,
        shadow=False,
    ):
        self.calls.append((max_new, shadow))
        ids = []
        for _ in range(min(self.yield_n, max_new)):
            if shadow:
                lbc.record_shadow_lesson({"session_id": source_session_id})
                ids.append(jm.SHADOW_ID)
            else:
                await _add_lesson(self.db, session=source_session_id)
                ids.append("x")
        return ids


def _fake_parse(monkeypatch, builder):
    monkeypatch.setattr(sd, "build_spine_and_haystack", lambda p: ([{"turn": 1}], "hay"))
    monkeypatch.setattr(sd, "score_struggle", lambda spine: 0.9)
    monkeypatch.setattr(jm, "judge_multi_procedure", builder)


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(lbc, "load_config", lambda: {"mode": mode})


async def _seed_session(db, tmp_path):
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, cc_session_id, source_tag,"
        " started_at, last_activity_at, status, last_extracted_line)"
        " VALUES ('s1', 'foreground', 'm', ?, 'foreground', ?, ?, 'active', 0)",
        (CC, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    await db.commit()
    _append(tmp_path, 2)


def _append(tmp_path, n):
    path = tmp_path / f"{CC}.jsonl"
    with path.open("a") as f:
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
        db=db,
        store=AsyncMock(),
        router=router,
        transcript_dir=tmp_path,
    )


async def _lessons(db):
    return (await (await db.execute("SELECT COUNT(*) FROM procedural_memory")).fetchone())[0]


# ── the cap ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cap_holds_across_cycles(db, tmp_path, monkeypatch):
    _set_mode(monkeypatch, "live")
    builder = _Builder(db)
    _fake_parse(monkeypatch, builder)
    await _seed_session(db, tmp_path)
    await _cycle(db, tmp_path)
    _append(tmp_path, 2)  # the session keeps going into the next cycle
    await _cycle(db, tmp_path)
    assert builder.calls == [(7, False)]  # the second cycle got no fresh allowance
    assert await _lessons(db) == 7


@pytest.mark.asyncio
async def test_cap_passes_only_the_remainder(db, tmp_path, monkeypatch):
    _set_mode(monkeypatch, "live")
    builder = _Builder(db)
    _fake_parse(monkeypatch, builder)
    for _ in range(5):
        await _add_lesson(db)
    await _seed_session(db, tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls == [(2, False)]


@pytest.mark.asyncio
async def test_lessons_older_than_the_window_do_not_count(db, monkeypatch):
    for _ in range(7):
        await _add_lesson(db, age=timedelta(hours=25))
    await _add_lesson(db, age=timedelta(hours=23))
    assert await ej._lesson_cap_left(db, CC, 7) == 6


@pytest.mark.asyncio
async def test_other_sessions_and_malformed_sources_do_not_count(db):
    await _add_lesson(db, session="other")
    await _add_lesson(db, source="not json")
    await _add_lesson(db, source=json.dumps(["a", CC]))
    await _add_lesson(db, source="null")
    await _add_lesson(db, source=None)
    assert await ej._lesson_cap_left(db, CC, 7) == 7


# ── the drain ───────────────────────────────────────────────────────────────


async def _drain(db, monkeypatch, builder):
    monkeypatch.setattr(ej, "_find_transcript", lambda d, s: Path("/fake/x.jsonl"))
    _fake_parse(monkeypatch, builder)
    await ej._drain_procedure_rebuilds(
        db=db,
        router=MagicMock(),
        transcript_dir=Path("/fake"),
        summary={},
        max_procedures_per_session=7,
    )
    return await dw_crud.query_pending(db, work_type="procedure_rebuild", limit=50)


@pytest.mark.asyncio
async def test_drain_passes_only_the_remainder(db, monkeypatch):
    _set_mode(monkeypatch, "live")
    for _ in range(4):
        await _add_lesson(db)
    await ej._enqueue_procedure_rebuild(db, "s1", CC)
    builder = _Builder(db)
    assert await _drain(db, monkeypatch, builder) == []
    assert builder.calls == [(3, False)]


@pytest.mark.asyncio
async def test_drain_leaves_a_capped_session_pending_without_spending_an_attempt(
    db,
    monkeypatch,
):
    _set_mode(monkeypatch, "live")
    for _ in range(7):
        await _add_lesson(db)
    await ej._enqueue_procedure_rebuild(db, "s1", CC)
    builder = _Builder(db)
    pending = await _drain(db, monkeypatch, builder)
    assert builder.calls == []
    assert len(pending) == 1 and pending[0]["attempts"] == 0


# ── the lever ───────────────────────────────────────────────────────────────


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    d = tmp_path / "out"
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(d))
    return d


def _watermark_moved(row):
    return row[0] > 0


@pytest.mark.asyncio
async def test_off_makes_no_builder_call_and_still_advances_the_watermark(
    db, tmp_path, monkeypatch, out_dir,
):
    _set_mode(monkeypatch, "off")
    builder = _Builder(db)
    _fake_parse(monkeypatch, builder)
    await _seed_session(db, tmp_path)
    await _cycle(db, tmp_path)
    assert builder.calls == []
    assert await _lessons(db) == 0
    row = await (await db.execute(
        "SELECT last_extracted_line FROM cc_sessions WHERE id='s1'"
    )).fetchone()
    assert _watermark_moved(row)
    assert not (out_dir / lbc.SHADOW_FILE).exists()


@pytest.mark.asyncio
async def test_shadow_runs_the_builder_records_and_stores_nothing(
    db, tmp_path, monkeypatch, out_dir,
):
    _set_mode(monkeypatch, "shadow")
    builder = _Builder(db)
    _fake_parse(monkeypatch, builder)
    await _seed_session(db, tmp_path)
    summary = await _cycle(db, tmp_path)
    assert builder.calls == [(7, True)]
    assert await _lessons(db) == 0
    assert summary["shadow_lessons"] == 7
    assert len((out_dir / lbc.SHADOW_FILE).read_text().splitlines()) == 7
    row = await (await db.execute(
        "SELECT last_extracted_line FROM cc_sessions WHERE id='s1'"
    )).fetchone()
    assert _watermark_moved(row)


@pytest.mark.asyncio
async def test_shadow_counts_its_own_records_against_the_cap(
    db, tmp_path, monkeypatch, out_dir,
):
    """Shadow stores no rows, so without counting its record it would call the
    builder every cycle for a long session, more often than live would."""
    _set_mode(monkeypatch, "shadow")
    builder = _Builder(db)
    _fake_parse(monkeypatch, builder)
    await _seed_session(db, tmp_path)
    await _cycle(db, tmp_path)
    _append(tmp_path, 2)
    await _cycle(db, tmp_path)
    assert builder.calls == [(7, True)]


def test_shadow_count_reads_both_files_and_skips_bad_lines(out_dir):
    out_dir.mkdir()
    now = datetime.now(UTC)
    old = (now - timedelta(hours=25)).isoformat()
    (out_dir / (lbc.SHADOW_FILE + ".1")).write_text(
        json.dumps({"ts": now.isoformat(), "session_id": CC}) + "\n"
        + json.dumps({"ts": old, "session_id": CC}) + "\n"
    )
    (out_dir / lbc.SHADOW_FILE).write_text(
        "not json\n"
        + json.dumps({"session_id": CC}) + "\n"
        + json.dumps({"ts": now.isoformat(), "session_id": "other"}) + "\n"
        + json.dumps({"ts": now.isoformat(), "session_id": CC}) + "\n"
    )
    assert lbc.shadow_count(CC, now - timedelta(hours=24)) == 2


def test_shadow_record_rotates_past_its_size_bound(out_dir, monkeypatch):
    monkeypatch.setattr(lbc, "SHADOW_MAX_BYTES", 100)
    for _ in range(5):
        lbc.record_shadow_lesson({"session_id": CC, "pad": "x" * 40})
    assert (out_dir / (lbc.SHADOW_FILE + ".1")).exists()
    assert (out_dir / lbc.SHADOW_FILE).stat().st_size <= 200


def test_shadow_record_write_failure_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(blocker / "sub"))
    lbc.record_shadow_lesson({"session_id": CC})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_off_and_shadow_leave_queued_rebuilds_untouched(db, monkeypatch, mode, out_dir):
    _set_mode(monkeypatch, mode)
    await ej._enqueue_procedure_rebuild(db, "s1", CC)
    builder = _Builder(db)
    pending = await _drain(db, monkeypatch, builder)
    assert builder.calls == []
    assert len(pending) == 1 and pending[0]["attempts"] == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("live", "live"),
        ("shadow", "shadow"),
        ("off", "off"),
        (False, "off"),
        ("on", "shadow"),
        (None, "shadow"),
        (3, "shadow"),
    ],
)
def test_builder_mode_reads_and_degrades_to_shadow(monkeypatch, value, expected):
    _set_mode(monkeypatch, value)
    assert lbc.builder_mode() == expected


def test_shipped_base_config_is_live():
    assert lbc.load_config()["mode"] == "live"


def test_settings_domain_validates_the_lever():
    from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _validate_lesson_builder

    assert _DOMAIN_REGISTRY["lesson_builder"].config_filename == "lesson_builder.yaml"
    assert _validate_lesson_builder({"mode": "shadow"}) == []
    assert _validate_lesson_builder({"mode": "on"})
    assert _validate_lesson_builder({"cap": 3})


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "queued"), [("shadow", 0), ("live", 1)])
async def test_a_failed_shadow_build_is_not_queued_for_a_later_live_store(
    db, tmp_path, monkeypatch, out_dir, mode, queued,
):
    _set_mode(monkeypatch, mode)
    _fake_parse(monkeypatch, AsyncMock(side_effect=jm.ProcedureBuilderUnavailable("down")))
    await _seed_session(db, tmp_path)
    summary = await _cycle(db, tmp_path)
    pending = await dw_crud.query_pending(db, work_type="procedure_rebuild", limit=50)
    assert len(pending) == queued
    assert summary["sessions_processed"] == 1


@pytest.mark.parametrize(
    "overlay", ["mode: off\n  bad: [indent\n", "- a list\n"],
)
def test_an_unreadable_overlay_reads_as_shadow_not_the_live_base(tmp_path, monkeypatch, overlay):
    import genesis._config_overlay as co

    monkeypatch.setattr(co, "_user_config_dir", lambda: tmp_path)
    (tmp_path / "lesson_builder.local.yaml").write_text(overlay)
    assert lbc.builder_mode() == "shadow"


def test_a_valid_overlay_is_honoured(tmp_path, monkeypatch):
    import genesis._config_overlay as co

    monkeypatch.setattr(co, "_user_config_dir", lambda: tmp_path)
    (tmp_path / "lesson_builder.local.yaml").write_text("mode: 'off'\n")
    assert lbc.builder_mode() == "off"


def test_an_unreadable_base_reads_as_shadow(tmp_path, monkeypatch):
    monkeypatch.setattr(lbc, "_base_path", lambda: tmp_path / "missing.yaml")
    assert lbc.builder_mode() == "shadow"
