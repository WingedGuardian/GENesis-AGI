"""Shipped lessons from config/lessons/*.yaml (advisory-lessons T-6)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from genesis.learning.procedural import trigger_cache

_SPEC = importlib.util.spec_from_file_location(
    "_seed_procedures_under_test",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_procedures.py",
)
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)

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


@pytest.fixture
def lessons(tmp_path) -> Path:
    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "deploy_detach.yaml").write_text(_GOOD)
    return directory


async def _rows(db) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM procedural_memory WHERE lesson IS NOT NULL")
    return {r["id"]: dict(r) for r in rows}


async def test_a_lesson_file_is_seeded_reviewed_and_cached_unlabelled(db, lessons):
    assert await seed.seed_shipped_lessons(db, lessons) == []
    (row,) = (await _rows(db)).values()
    lesson = json.loads(row["lesson"])
    assert (lesson["authored_via"], lesson["reviewed"], row["activation_tier"]) == (
        "shipped",
        True,
        "ADVISORY",
    )
    assert json.loads(row["trigger_spec"]) == {
        "patterns": [r"deploy_code_only\.sh"],
        "repeat": "session",
    }
    await trigger_cache.regenerate(db)
    entry = json.loads(trigger_cache.get_json_cache_path().read_text())["triggers"][0]
    assert (entry["procedure_id"], entry["kind"]) == (row["id"], "lesson")
    assert "label" not in entry


async def test_a_rerun_changes_nothing(db, lessons):
    await seed.seed_shipped_lessons(db, lessons)
    first = await _rows(db)
    await seed.seed_shipped_lessons(db, lessons)
    assert await _rows(db) == first


async def test_one_bad_file_is_skipped_and_the_others_still_seed(db, lessons):
    (lessons / "broken.yaml").write_text("task_type: x\nprinciple: y\n")  # missing fields
    (lessons / "bad_tool.yaml").write_text(_GOOD.replace("[Bash]", "[ssh-ish]"))
    (lessons / "bad_regex.yaml").write_text(_GOOD.replace("deploy_code_only\\.sh", "("))
    errors = await seed.seed_shipped_lessons(db, lessons)
    assert sorted(e.split(":")[0] for e in errors) == [
        "bad_regex.yaml",
        "bad_tool.yaml",
        "broken.yaml",
    ]
    assert len(await _rows(db)) == 1


async def test_removing_a_file_deprecates_and_restoring_it_revives(db, lessons):
    await seed.seed_shipped_lessons(db, lessons)
    (row_id,) = await _rows(db)
    (lessons / "deploy_detach.yaml").unlink()
    await seed.seed_shipped_lessons(db, lessons)
    assert (await _rows(db))[row_id]["deprecated"] == 1
    (lessons / "deploy_detach.yaml").write_text(_GOOD)
    await seed.seed_shipped_lessons(db, lessons)
    assert (await _rows(db))[row_id]["deprecated"] == 0


async def test_an_invalid_edit_keeps_the_earlier_row(db, lessons):
    await seed.seed_shipped_lessons(db, lessons)
    before = await _rows(db)
    (lessons / "deploy_detach.yaml").write_text("not: [valid")
    assert len(await seed.seed_shipped_lessons(db, lessons)) == 1
    assert await _rows(db) == before


async def test_a_missing_directory_deprecates_nothing(db, lessons, tmp_path):
    await seed.seed_shipped_lessons(db, lessons)
    before = await _rows(db)
    errors = await seed.seed_shipped_lessons(db, tmp_path / "absent")
    assert len(errors) == 1
    assert await _rows(db) == before


def test_the_shipped_directory_holds_only_valid_lessons():
    for path in sorted(seed.LESSONS_DIR.glob("*.yaml")):
        seed._lesson_from_file(path)


async def test_an_owner_deprecation_survives_a_re_seed(db, lessons):
    from genesis.db.crud import procedural

    await seed.seed_shipped_lessons(db, lessons)
    (row_id,) = await _rows(db)
    await procedural.update(db, row_id, deprecated=1, deprecated_reason="owner: noisy")
    await seed.seed_shipped_lessons(db, lessons)
    row = (await _rows(db))[row_id]
    assert (row["deprecated"], row["deprecated_reason"]) == (1, "owner: noisy")


async def test_a_re_seed_keeps_usage_and_clears_a_stale_embedding(db, lessons):
    from genesis.db.crud import procedural

    await seed.seed_shipped_lessons(db, lessons)
    (row_id,) = await _rows(db)
    await procedural.update(
        db, row_id, success_count=5, confidence=0.4, version=3, principle_embedding=b"x"
    )
    await seed.seed_shipped_lessons(db, lessons)
    row = (await _rows(db))[row_id]
    assert (row["success_count"], row["confidence"], row["version"]) == (5, 0.4, 3)
    assert row["principle_embedding"] == b"x"  # unchanged text keeps its vector
    (lessons / "deploy_detach.yaml").write_text(
        _GOOD.replace("Launch a long deploy", "Start a long deploy")
    )
    await seed.seed_shipped_lessons(db, lessons)
    assert (await _rows(db))[row_id]["principle_embedding"] is None


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("cli_word.yaml", _GOOD.replace("[Bash]", "[Bash, ssh]")),
        ("duplicate.yaml", _GOOD + "repeat: every\n"),
        ("deep.yaml", "task_type: " + "[" * 5000 + "]" * 5000 + "\n"),
        ("wrong.yml", _GOOD),
        ("mcp_short.yaml", _GOOD.replace("[Bash]", "[browser_navigate]")),
        ("scalar.yaml", _GOOD.replace("[Bash]", "Bash")),
    ],
)
async def test_more_invalid_files_are_reported(db, lessons, name, text):
    (lessons / name).write_text(text)
    errors = await seed.seed_shipped_lessons(db, lessons)
    assert [e.split(":")[0] for e in errors] == [name]
    assert len(await _rows(db)) == 1


async def test_a_re_seed_clears_a_spec_left_on_a_seed_row(tmp_path):
    import aiosqlite

    path = tmp_path / "genesis.db"
    await seed.main(path)
    seed_id = seed.SEED_PROCEDURES[0]["id"]
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE procedural_memory SET trigger_spec = ? WHERE id = ?",
            (json.dumps({"patterns": [], "commands": ["ssh"], "repeat": "every"}), seed_id),
        )
        await db.commit()
    await seed.main(path)  # restores the seed's trigger, so its spec must go too
    async with aiosqlite.connect(path) as db:
        rows = await db.execute_fetchall(
            "SELECT trigger_spec FROM procedural_memory WHERE id = ?", (seed_id,)
        )
    assert rows[0][0] is None


async def test_the_last_line_names_every_skipped_file(tmp_path, monkeypatch, capsys):
    async def two_errors(_db):
        return ["a.yaml: bad", "b.yaml: worse"]

    monkeypatch.setattr(seed, "seed_shipped_lessons", two_errors)
    await seed.main(tmp_path / "genesis.db")
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "a.yaml" in last and "b.yaml" in last


async def test_a_rename_to_yml_keeps_the_lesson(db, lessons):
    await seed.seed_shipped_lessons(db, lessons)
    (lessons / "deploy_detach.yaml").rename(lessons / "deploy_detach.yml")
    errors = await seed.seed_shipped_lessons(db, lessons)
    assert errors == ["deploy_detach.yml: use the .yaml extension"]
    (row,) = (await _rows(db)).values()
    assert row["deprecated"] == 0  # reported, not retired


async def test_unreadable_stored_values_are_repaired_and_workarounds_kept(db, lessons):
    await seed.seed_shipped_lessons(db, lessons)
    (row_id,) = await _rows(db)
    await db.execute(
        "UPDATE procedural_memory SET lesson = '{not json', attempted_workarounds = ? WHERE id = ?",
        (json.dumps(["retry with setsid"]), row_id),
    )
    await db.commit()
    assert await seed.seed_shipped_lessons(db, lessons) == []
    row = (await _rows(db))[row_id]
    assert json.loads(row["lesson"])["authored_via"] == "shipped"
    assert json.loads(row["attempted_workarounds"]) == ["retry with setsid"]


@pytest.mark.parametrize("stored", [None, 5, ["x"], "not a date"])
async def test_an_unreadable_authored_at_is_replaced_so_the_lesson_stays_live(db, lessons, stored):
    from genesis.learning.procedural.lesson_shape import is_live_lesson

    await seed.seed_shipped_lessons(db, lessons)
    (row_id,) = await _rows(db)
    record = json.loads((await _rows(db))[row_id]["lesson"])
    await db.execute(
        "UPDATE procedural_memory SET lesson = ? WHERE id = ?",
        (json.dumps({**record, "authored_at": stored}), row_id),
    )
    await db.commit()
    await seed.seed_shipped_lessons(db, lessons)
    assert is_live_lesson((await _rows(db))[row_id]["lesson"])
