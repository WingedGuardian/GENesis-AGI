"""``python -m genesis lessons`` (advisory-lessons T-7)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiosqlite
import pytest

from genesis.learning.procedural import lessons_cli
from genesis.learning.procedural.embedding import pack_embedding
from genesis.learning.procedural.lesson_shape import HELD_REASON, SHIPPED_RETIRED_REASON

_REPO = Path(__file__).resolve().parents[2]
_VEC = pack_embedding([1.0] + [0.0] * 1023)
_OTHER = pack_embedding([0.0, 1.0] + [0.0] * 1022)
_NOW = "2026-10-14T00:00:00+00:00"


def _lesson(origin="first_party", reviewed=False, via="mcp") -> str:
    return json.dumps(
        {
            "authored_via": via,
            "authored_in": "foreground",
            "origin": origin,
            "reviewed": reviewed,
            "authored_at": _NOW,
        }
    )


async def _build(path: Path, rows: list[dict]) -> None:
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(path) as db:
        await create_all_tables(db)
        for r in rows:
            await db.execute(
                "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags,"
                " created_at, activation_tier, tool_trigger, confidence, trigger_spec, lesson, deprecated,"
                " deprecated_reason, quarantined, principle_embedding) VALUES"
                " (?, ?, ?, '[\"s\"]', '[\"Bash\"]', '[\"t\"]', ?, ?, '[\"Bash\"]', 0.9, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"],
                    r.get("task_type", "deploy"),
                    r.get("principle", f"principle {r['id']}"),
                    _NOW,
                    r.get("tier", "ADVISORY"),
                    json.dumps({"patterns": ["deploy"], "repeat": "session"}),
                    r.get("lesson"),
                    r.get("deprecated", 0),
                    r.get("reason"),
                    r.get("quarantined", 0),
                    r.get("embedding", _OTHER),
                ),
            )
        await db.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    cache = tmp_path / "procedure_triggers.yaml"
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    # The suite's autouse isolation patches the resolver itself; point it here.
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
    monkeypatch.setenv("GENESIS_PROCEDURE_TRIGGERS", str(cache))
    monkeypatch.setenv("GENESIS_SESSIONS_DIR", str(sessions))
    from genesis.learning.procedural import trigger_cache

    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", cache)
    parser = argparse.ArgumentParser()
    lessons_cli.add_parser(parser.add_subparsers(dest="command"))

    def run(*argv: str) -> int:
        args = parser.parse_args(["lessons", *argv])
        return args.func(args)

    def build(*rows: dict) -> None:
        asyncio.run(_build(db, list(rows)))

    def cached() -> set[str]:
        path = cache.with_suffix(".json")
        return (
            {t["procedure_id"] for t in json.loads(path.read_text())["triggers"]}
            if path.exists()
            else set()
        )

    def row(pid: str) -> dict:
        async def read():
            async with aiosqlite.connect(db) as conn:
                conn.row_factory = aiosqlite.Row
                rows = await conn.execute_fetchall(
                    "SELECT * FROM procedural_memory WHERE id = ?", (pid,)
                )
                return dict(rows[0]) if rows else None

        return asyncio.run(read())

    run.build, run.cached, run.row, run.sessions, run.db = build, cached, row, sessions, db
    return run


_HELD = {
    "lesson": _lesson("external_untrusted"),
    "deprecated": 1,
    "reason": HELD_REASON,
    "tier": "LIBRARY",
}


def test_list_shows_every_state_and_counts_earned_rows_apart(env, capsys):
    env.build(
        {"id": "live", "lesson": _lesson()},
        {"id": "held", **_HELD},
        {
            "id": "gone",
            "lesson": _lesson(via="shipped"),
            "deprecated": 1,
            "reason": SHIPPED_RETIRED_REASON,
        },
        {"id": "earned", "lesson": None, "tier": "CORE"},
    )
    assert env("list") == 0
    out = capsys.readouterr().out
    assert "live  " in out and "held  " in out and f"deprecated ({SHIPPED_RETIRED_REASON})" in out
    assert "3 lesson(s); earned rows (not lessons): CORE 1" in out
    assert env("list", "--held") == 0
    assert "1 lesson(s)" in capsys.readouterr().out


def test_review_revives_a_held_lesson_into_the_cache(env):
    env.build({"id": "held", **_HELD})
    assert env("review", "held") == 0
    row = env.row("held")
    assert (row["deprecated"], row["deprecated_reason"], row["activation_tier"]) == (
        0,
        None,
        "ADVISORY",
    )
    assert json.loads(row["lesson"])["reviewed"] is True
    assert "held" in env.cached()
    assert env("review", "held") == 0  # already live: reviewing again is harmless


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ({"lesson": _lesson(), "deprecated": 1, "reason": "owner: noisy"}, 1),
        ({**_HELD, "quarantined": 1}, 1),
        ({"lesson": _lesson(via="shipped"), "deprecated": 1, "reason": SHIPPED_RETIRED_REASON}, 1),
        ({"lesson": None}, 2),
    ],
)
def test_review_revives_only_a_held_lesson(env, row, code):
    env.build({"id": "x", **row})
    assert env("review", "x") == code
    assert env.row("x")["deprecated"] == row.get("deprecated", 0)


def test_review_refuses_a_twin_or_an_unchecked_row_unless_forced(env, capsys):
    env.build(
        {"id": "live", "lesson": _lesson(), "embedding": _VEC},
        {"id": "held", **_HELD, "embedding": _VEC},
        {"id": "bare", **_HELD, "embedding": None},
    )
    assert env("review", "held") == 1
    assert "already teaches this" in capsys.readouterr().out
    assert env("review", "bare") == 1
    assert "twin check unavailable" in capsys.readouterr().out
    assert env("review", "bare", "--force") == 0
    assert env.row("bare")["deprecated"] == 0
    assert env("review", "nope") == 2


def test_review_holds_the_write_lock_through_the_twin_check(env, monkeypatch):
    import sqlite3

    from genesis.db.crud import procedural

    env.build({"id": "held", **_HELD, "embedding": _VEC})
    real = procedural.list_by_task_type
    blocked = []

    async def twin_check_with_a_racing_writer(db, *args, **kwargs):
        # Another writer tries to commit a live twin while the check reads.
        async with aiosqlite.connect(env.db, timeout=0) as other:
            try:
                await other.execute(
                    "UPDATE procedural_memory SET principle = 'raced' WHERE id = 'held'"
                )
                await other.commit()
            except sqlite3.OperationalError as exc:
                blocked.append(str(exc))
        return await real(db, *args, **kwargs)

    monkeypatch.setattr(procedural, "list_by_task_type", twin_check_with_a_racing_writer)
    assert env("review", "held") == 0
    assert blocked and "locked" in blocked[0]


def test_a_twin_refusal_leaves_the_database_writable(env):
    env.build(
        {"id": "live", "lesson": _lesson(), "embedding": _VEC},
        {"id": "held", **_HELD, "embedding": _VEC},
    )
    assert env("review", "held") == 1  # the twin refusal returns inside the lock
    assert env("deprecate", "live", "--reason", "owner: superseded") == 0


def test_deprecate_takes_a_lesson_out_of_service(env):
    env.build({"id": "live", "lesson": _lesson()})
    assert env("review", "live") == 0 and "live" in env.cached()
    assert env("deprecate", "live", "--reason", HELD_REASON) == 2  # reserved
    assert env("deprecate", "live", "--reason", "wrong advice") == 0
    assert (env.row("live")["deprecated"], env.row("live")["deprecated_reason"]) == (
        1,
        "wrong advice",
    )
    assert "live" not in env.cached()


def test_a_copy_of_the_database_never_writes_the_live_cache(env, monkeypatch, capsys):
    env.build({"id": "held", **_HELD})
    monkeypatch.delenv("GENESIS_PROCEDURE_TRIGGERS")
    assert env("review", "held") == 4  # written, cache left alone
    assert env.row("held")["deprecated"] == 0
    assert "cache not rebuilt" in capsys.readouterr().out


def test_sample_reads_recent_fires_newest_first(env, capsys):
    env.build({"id": "live", "lesson": _lesson()})
    now = time.time()
    recent = env.sessions / "sess-new"
    recent.mkdir(parents=True)
    (recent / "advisor_fires.jsonl").write_text(
        json.dumps(
            {
                "ts": now - 60,
                "procedure_id": "live",
                "agent": "main",
                "tool": "Bash",
                "excerpt": "deploy [REDACTED]",
            }
        )
        + "\n"
        + json.dumps({"ts": now - 30, "procedure_id": "gone", "agent": "a1", "tool": "Bash"})
        + "\n"
        + '{"ts": '
        + str(now)  # a partly written last line
    )
    old = env.sessions / "sess-old"
    old.mkdir()
    (old / "advisor_fires.jsonl").write_text(
        json.dumps({"ts": now - 90 * 86400, "procedure_id": "live"}) + "\n"
    )
    os.utime(old / "advisor_fires.jsonl", (now - 90 * 86400, now - 90 * 86400))
    assert env("sample", "--days", "14") == 0
    out = capsys.readouterr().out
    assert out.startswith("2 fire(s)")
    assert out.index("(row gone)") < out.index("deploy [REDACTED]")  # newest first
    assert "(no excerpt)" in out


def test_the_command_is_wired_into_python_m_genesis(env, tmp_path):
    env.build({"id": "live", "lesson": _lesson()})
    result = subprocess.run(
        [sys.executable, "-m", "genesis", "lessons", "list"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(_REPO / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "1 lesson(s)" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_seed_main_exits_when_it_fails(tmp_path):
    """A failing seed closes its connection, so the process ends instead of hanging."""
    db = tmp_path / "ro.db"
    asyncio.run(_build(db, []))
    db.chmod(0o444)
    result = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "seed_procedures.py"), "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PYTHONPATH": str(_REPO / "src"),
            "GENESIS_PROCEDURE_TRIGGERS": str(tmp_path / "pt.yaml"),
        },
    )
    assert result.returncode != 0


def test_a_held_row_with_a_broken_record_is_refused_and_stays_held(env):
    env.build(
        {"id": "bad_json", **_HELD, "lesson": "{not json"},
        {"id": "bad_record", **_HELD, "lesson": json.dumps({"origin": "external_untrusted"})},
    )
    for pid in ("bad_json", "bad_record"):
        assert env("review", pid, "--force") == 1
        assert env.row(pid)["deprecated"] == 1  # rolled back: still held


def test_a_fenced_database_is_never_opened(env, monkeypatch):
    def fenced(_path):
        raise RuntimeError("quarantined")

    monkeypatch.setattr("genesis.db.admission.assert_admitted", fenced)
    assert env("list") == 3


def test_a_missing_database_is_not_found(env, monkeypatch, tmp_path):
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: tmp_path / "absent.db")
    assert env("list") == 2


def test_sample_skips_foreign_records(env, capsys):
    env.build({"id": "live", "lesson": _lesson()})
    log = env.sessions / "sess-x" / "advisor_fires.jsonl"
    log.parent.mkdir(parents=True)
    now = time.time()
    log.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"ts": "soon", "procedure_id": "live"},
                {"ts": 1e300, "procedure_id": "live"},
                {"ts": now, "procedure_id": ["live"]},
                {"ts": now, "procedure_id": "live"},
            )
        )
        + "\n"
    )
    assert env("sample") == 0
    assert capsys.readouterr().out.startswith("1 fire(s)")


def test_review_refuses_rows_it_cannot_check_or_cache(env):
    env.build(
        {"id": "bad_vec", **_HELD, "embedding": b"not a vector"},
        {"id": "no_tools", **_HELD},
    )
    assert env("review", "bad_vec") == 1  # unpackable embedding: no twin check without --force
    asyncio.run(_set(env, "no_tools", "tool_trigger", "[]"))
    assert env("review", "no_tools", "--force") == 1  # it would be live but never cached
    assert env.row("no_tools")["deprecated"] == 1


def test_a_malformed_sibling_is_no_twin(env):
    env.build(
        {"id": "odd", "lesson": json.dumps({"origin": "x"}), "embedding": _VEC},
        {"id": "held", **_HELD, "embedding": _VEC},
    )
    assert env("review", "held") == 0


@pytest.mark.parametrize("days", ["-1", "nan", "inf", "0"])
def test_sample_rejects_a_bad_window(env, days):
    with pytest.raises(SystemExit):
        env("sample", "--days", days)


def test_sample_survives_a_huge_integer_ts(env, capsys):
    env.build({"id": "live", "lesson": _lesson()})
    log = env.sessions / "s" / "advisor_fires.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"ts": ' + "9" * 400 + ', "procedure_id": "live"}\n')
    assert env("sample") == 0


async def _set(env, pid, column, value):
    async with aiosqlite.connect(env.db) as conn:
        await conn.execute(f"UPDATE procedural_memory SET {column} = ? WHERE id = ?", (value, pid))
        await conn.commit()
