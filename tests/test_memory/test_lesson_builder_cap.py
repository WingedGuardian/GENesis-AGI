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


async def _create_lesson(db, *, session=CC, source=_DEFAULT):
    """Insert through the store's own create, which sets created_session_id."""
    from genesis.db.crud import procedural

    if source is _DEFAULT:
        source = json.dumps({"type": "struggle_extraction", "session_id": session})
    return await procedural.create(
        db, id=uuid.uuid4().hex, task_type="t", principle="p", steps=[], tools_used=[],
        context_tags=[], created_at=datetime.now(UTC).isoformat(), source=source,
    )


@pytest.mark.asyncio
async def test_a_refine_that_rewrites_source_still_counts_against_the_creator(db):
    """Codex P2 on #4720: a refine overwrites ``source`` (operations.py), which
    reopened the creating session's slot when the cap counted by source."""
    from genesis.db.crud import procedural

    pid = await _create_lesson(db)
    await procedural.update(db, pid, source={"type": "explicit_teach"})
    assert await ej._lesson_cap_left(db, CC, 7) == 6
    await procedural.update(db, pid, source={"type": "struggle_extraction", "session_id": "x"})
    assert await ej._lesson_cap_left(db, "x", 7) == 7  # the refiner created nothing


@pytest.mark.asyncio
async def test_rows_without_a_creation_session_fall_back_to_source(db):
    await _add_lesson(db)  # a raw insert, as rows written before the column
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


@pytest.mark.asyncio
async def test_capped_rebuilds_do_not_starve_an_eligible_one_behind_them(db, monkeypatch):
    """Codex P2 + Devin on #4720: the drain read a 20-item page and skipped the
    capped items in it without moving them, so an eligible item behind 20 capped
    ones waited until a cap window expired."""
    _set_mode(monkeypatch, "live")
    for i in range(ej._DRAIN_BATCH):
        for _ in range(7):
            await _add_lesson(db, session=f"capped-{i}")
        await ej._enqueue_procedure_rebuild(db, f"s{i}", f"capped-{i}")
    await ej._enqueue_procedure_rebuild(db, "free", "free-session")
    seen = []
    builder = _Builder(db)

    async def spy(db_, spine, haystack, score, router, *, source_session_id, max_new, shadow=False):
        seen.append(source_session_id)
        return await builder(
            db_, spine, haystack, score, router,
            source_session_id=source_session_id, max_new=max_new, shadow=shadow,
        )

    pending = await _drain(db, monkeypatch, spy)
    assert seen == ["free-session"]
    assert len(pending) == ej._DRAIN_BATCH
    assert all(p["attempts"] == 0 for p in pending)


@pytest.mark.asyncio
async def test_the_drain_still_processes_at_most_one_batch(db, monkeypatch):
    _set_mode(monkeypatch, "live")
    for i in range(ej._DRAIN_BATCH + 3):
        await ej._enqueue_procedure_rebuild(db, f"s{i}", f"open-{i}")
    builder = _Builder(db, yield_n=0)
    pending = await _drain(db, monkeypatch, builder)
    assert len(builder.calls) == ej._DRAIN_BATCH
    assert len(pending) == 3


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


def _write_records(path, *ages):
    now = datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"ts": (now - age).isoformat(), "session_id": CC}) + "\n" for age in ages
    ))


def test_shadow_record_rotates_only_once_its_oldest_record_leaves_the_window(out_dir):
    """Rotation replaces ``.1``; it may do so only when every record it would
    drop is outside the cap window, or the cap forgets lessons still inside it."""
    active, rotated = out_dir / lbc.SHADOW_FILE, out_dir / (lbc.SHADOW_FILE + ".1")
    _write_records(rotated, timedelta(hours=2))
    _write_records(active, timedelta(hours=3), timedelta(hours=1))
    lbc.record_shadow_lesson({"session_id": CC})
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 4  # nothing rotated
    _write_records(active, lbc.CAP_WINDOW + timedelta(minutes=5), timedelta(hours=1))
    lbc.record_shadow_lesson({"session_id": CC})
    assert len(rotated.read_text().splitlines()) == 2  # the old active file is now .1
    assert len(active.read_text().splitlines()) == 1
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 2


def test_shadow_rotation_skips_unreadable_leading_lines(out_dir):
    active = out_dir / lbc.SHADOW_FILE
    _write_records(active, lbc.CAP_WINDOW + timedelta(hours=1))
    active.write_text("not json\n" + active.read_text())
    lbc.record_shadow_lesson({"session_id": CC})
    assert (out_dir / (lbc.SHADOW_FILE + ".1")).exists()


def test_shadow_count_keeps_a_record_whose_text_holds_a_line_separator(out_dir):
    """``ensure_ascii=False`` writes U+2028 raw, and ``splitlines`` splits on it."""
    lbc.record_shadow_lesson({"session_id": CC, "principle": "a b c\x85d"})
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 1
    assert lbc.shadow_records(CC, datetime.now(UTC) - lbc.CAP_WINDOW)[0]["principle"] == (
        "a b c\x85d"
    )


def _unit(i):
    from genesis.learning.procedural.embedding import EMBEDDING_DIM

    vec = [0.0] * EMBEDDING_DIM
    vec[i] = 1.0
    return vec


async def _shadow_store(db, monkeypatch, *, session, vec, fell_open=False):
    import genesis.learning.procedural.extractor as ex
    import genesis.learning.procedural.scoping as sc

    monkeypatch.setattr(sc, "is_behavioral_directive", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_principle_is_novel", AsyncMock(return_value=(True, 0.1, vec, fell_open)),
    )
    return await jm._store_judged_procedure(
        db, {"task_type": f"t-{uuid.uuid4().hex[:6]}", "principle": "p", "steps": ["s"]},
        MagicMock(), source_type="struggle_extraction", source_session_id=session, shadow=True,
    )


@pytest.mark.asyncio
async def test_shadow_drops_a_lesson_it_already_recorded_for_the_session(db, monkeypatch, out_dir):
    """Devin on #4720: shadow records are not in the store, so the novelty gate
    could not see them, and each cycle's re-proposal of the same lesson used up
    the session's shadow cap. Live mode rejects that repeat at the store."""
    assert await _shadow_store(db, monkeypatch, session=CC, vec=_unit(0)) == jm.SHADOW_ID
    assert await _shadow_store(db, monkeypatch, session=CC, vec=_unit(0)) is None
    assert await _shadow_store(db, monkeypatch, session=CC, vec=_unit(1)) == jm.SHADOW_ID
    assert await _shadow_store(db, monkeypatch, session="other", vec=_unit(0)) == jm.SHADOW_ID
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 2


@pytest.mark.asyncio
async def test_shadow_records_a_lesson_it_cannot_compare(db, monkeypatch, out_dir):
    """With no usable vector (the embedder is down) the repeat check cannot run,
    and the lesson is recorded, as live's novelty gate fails open."""
    assert await _shadow_store(db, monkeypatch, session=CC, vec=None, fell_open=True) == jm.SHADOW_ID
    assert await _shadow_store(db, monkeypatch, session=CC, vec=None, fell_open=True) == jm.SHADOW_ID
    lbc.record_shadow_lesson({"session_id": CC, "principle_embedding_b64": "not base64!"})
    assert await _shadow_store(db, monkeypatch, session=CC, vec=_unit(0)) == jm.SHADOW_ID


@pytest.mark.asyncio
async def test_shadow_rate_limits_fail_open_records_as_live_does(db, monkeypatch, out_dir):
    """With the embedder down, live allows one store per task type per cooldown;
    shadow must not allow more, and must not limit a later live store."""
    import genesis.learning.procedural.embedding as emb
    import genesis.learning.procedural.extractor as ex
    import genesis.learning.procedural.scoping as sc

    monkeypatch.setattr(jm, "_shadow_fail_open_timestamps", {})
    monkeypatch.setattr(emb, "_fail_open_timestamps", {})
    monkeypatch.setattr(jm, "_fail_open_timestamps", emb._fail_open_timestamps)
    monkeypatch.setattr(sc, "is_behavioral_directive", AsyncMock(return_value=False))
    monkeypatch.setattr(ex, "_principle_is_novel", AsyncMock(return_value=(True, 0.0, None, True)))

    async def store(shadow):
        return await jm._store_judged_procedure(
            db, {"task_type": "same-type", "principle": "p", "steps": ["s"]}, MagicMock(),
            source_type="struggle_extraction", source_session_id=CC, shadow=shadow,
        )

    assert await store(True) == jm.SHADOW_ID
    assert await store(True) is None  # cooldown, as live
    assert emb._fail_open_timestamps == {}  # live untouched


def test_a_held_shadow_lock_makes_a_writer_wait(out_dir):
    import fcntl
    import threading

    out_dir.mkdir()
    lock = open(out_dir / (lbc.SHADOW_FILE + ".lock"), "a")  # noqa: SIM115
    fcntl.flock(lock, fcntl.LOCK_EX)
    writer = threading.Thread(target=lbc.record_shadow_lesson, args=({"session_id": CC},))
    writer.start()
    writer.join(timeout=0.5)
    try:
        assert writer.is_alive() and not (out_dir / lbc.SHADOW_FILE).exists()
    finally:
        lock.close()
    writer.join(timeout=10)
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 1


def test_a_record_cut_short_does_not_swallow_the_next_one(out_dir):
    out_dir.mkdir()
    (out_dir / lbc.SHADOW_FILE).write_text('{"ts": "2026-11-15T00:00:00+00:00", "sess')
    lbc.record_shadow_lesson({"session_id": CC})
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 1


def test_a_lock_that_cannot_be_taken_degrades_to_unlocked(out_dir, monkeypatch):
    import fcntl

    def refuse(*_a):
        raise OSError("ENOLCK")

    lbc.record_shadow_lesson({"session_id": CC})
    monkeypatch.setattr(fcntl, "flock", refuse)
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 1
    lbc.record_shadow_lesson({"session_id": CC})
    assert lbc.shadow_count(CC, datetime.now(UTC) - lbc.CAP_WINDOW) == 2


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


@pytest.mark.parametrize("text", ["false\n", "0\n", "[]\n", "''\n", "", "# only a comment\n"])
def test_a_base_that_is_not_a_mapping_reads_as_shadow(tmp_path, monkeypatch, text):
    """A falsy root (false, 0, [], an empty string) must not become an empty
    mapping, which would merge to the live default; an empty base is damaged too."""
    base = tmp_path / "lesson_builder.yaml"
    base.write_text(text)
    monkeypatch.setattr(lbc, "_base_path", lambda: base)
    assert lbc.builder_mode() == "shadow"
