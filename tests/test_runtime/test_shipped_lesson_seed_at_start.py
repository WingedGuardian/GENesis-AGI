"""Shipped lessons are seeded at genesis-server start (runtime/init/learning.py)."""

from __future__ import annotations

import functools
import importlib.util
import json
import logging
import uuid
from pathlib import Path

import pytest

from genesis.learning.procedural import shipped_lessons, trigger_cache
from genesis.runtime.init import learning

_GOOD = """\
task_type: deploy_detach
principle: Launch a long deploy detached.
steps: [Use setsid.]
tool_trigger: [Bash]
patterns: ['deploy_code_only\\.sh']
repeat: session
"""


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", tmp_path / "procedure_triggers.yaml")


async def test_a_start_seeds_the_lesson_and_puts_it_in_the_cache(db, tmp_path, monkeypatch):
    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "deploy_detach.yaml").write_text(_GOOD)
    monkeypatch.setattr(
        shipped_lessons,
        "seed_shipped_lessons",
        functools.partial(shipped_lessons.seed_shipped_lessons, directory=directory),
    )
    await shipped_lessons.seed_at_start(db)
    triggers = json.loads(trigger_cache.get_json_cache_path().read_text())["triggers"]
    assert [t["kind"] for t in triggers] == ["lesson"]
    await shipped_lessons.seed_at_start(db)  # a second start changes nothing
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM procedural_memory WHERE lesson IS NOT NULL"
    )
    assert rows[0][0] == 1


async def test_a_failing_seed_is_logged_and_reaches_its_task(db, monkeypatch, caplog):
    async def broken(_db):
        raise OSError("disk full")

    monkeypatch.setattr(shipped_lessons, "seed_shipped_lessons", broken)
    # Re-raised, so the tracked task running it reports it (task.failed).
    with (
        caplog.at_level(logging.ERROR, logger=shipped_lessons.logger.name),
        pytest.raises(OSError),
    ):
        await shipped_lessons.seed_at_start(db)
    assert "Shipped lesson seed at start failed" in caplog.text


async def test_skipped_files_are_logged(db, monkeypatch, caplog):
    async def one_error(_db):
        return ["bad.yaml: not a YAML mapping"]

    monkeypatch.setattr(shipped_lessons, "seed_shipped_lessons", one_error)
    with caplog.at_level(logging.WARNING, logger=shipped_lessons.logger.name):
        await shipped_lessons.seed_at_start(db)
    assert "bad.yaml" in caplog.text


async def test_the_learning_init_starts_the_seed_even_without_a_router(monkeypatch):
    import genesis.util.tasks as tasks

    started = []

    def record(coro, *, name=None, **_kw):
        started.append(name)
        coro.close()

    monkeypatch.setattr(tasks, "tracked_task", record)

    class _RT:
        _db = object()
        _router = None  # a router-degraded start returns early from the step

    await learning.init(_RT())
    assert started == ["shipped_lesson_seed"]


def test_the_seed_script_and_the_server_share_one_implementation_and_its_ids():
    spec = importlib.util.spec_from_file_location(
        "_seed_procedures",
        Path(__file__).resolve().parents[2] / "scripts" / "seed_procedures.py",
    )
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert seed.seed_shipped_lessons is shipped_lessons.seed_shipped_lessons
    assert seed.LESSONS_DIR == shipped_lessons.LESSONS_DIR
    assert shipped_lessons.LESSONS_DIR.is_dir()
    # Ids from before the move: a changed namespace would orphan every seeded row.
    namespace = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert shipped_lessons._lesson_id(Path("x.yaml")) == str(uuid.uuid5(namespace, "lesson:x"))
    assert seed._id("t") == str(uuid.uuid5(namespace, "t"))
