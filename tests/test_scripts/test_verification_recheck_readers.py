"""Due recheck backlog and structured log on disposable ledgers."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import aiosqlite
import pytest

from tests.test_scripts.test_verification_readers import (
    NOW,
    REPO,
    _scoped_records,
    _w,
    build_pr_verifications,
)

_EVENT = {"trigger": "the next weekly run", "recheck": "journalctl -n 20", "detail": "job ran?"}
_PAST = {"trigger": "2026-01-02", "recheck": "echo synthetic", "detail": "dated"}
_FUTURE = {"trigger": "2999-01-01", "recheck": "echo later", "detail": "not yet"}


@pytest.mark.parametrize("column", ["evidence", "rechecks"])
def test_log_keeps_raw_record_when_decode_succeeds_but_indented_encode_fails(
    tmp_path, capsys, column
):
    import sys

    db = tmp_path / "synthetic.db"
    _build_rechecks(db, [_EVENT])

    def serialization_probe(raw):
        try:
            parsed = json.loads(raw)
        except RecursionError:
            return False
        try:
            json.dumps({column: parsed}, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        except RecursionError:
            return True
        return False

    def decode_succeeds_encode_fails(raw):
        # Match the log entry point plus its record formatter's call depth.
        return serialization_probe(raw)

    previous_limit = sys.getrecursionlimit()
    try:
        # Control the interpreter boundary, then sweep instead of guessing a
        # magic nesting depth whose result depends on pytest's own call stack.
        sys.setrecursionlimit(1000)
        raw = None
        for depth in range(600, 1000):
            candidate = "[" * depth + "0" + "]" * depth
            if decode_succeeds_encode_fails(candidate):
                raw = candidate
                break
        assert raw is not None, "the controlled decoder/encoder boundary must be exercised"

        async def corrupt_and_add_neighbor():
            from genesis.db.crud import pr_verifications as crud

            async with aiosqlite.connect(db) as conn:
                await conn.execute(
                    f"UPDATE pr_verifications SET {column} = ? WHERE repo = ?", (raw, REPO)
                )
                await crud.open_verification(
                    conn,
                    repo="owner/z-next",
                    pr_number=40,
                    pr_title="synthetic",
                    merged_at=NOW,
                    now=NOW,
                )
                await crud.close_verification(
                    conn,
                    repo="owner/z-next",
                    pr_number=40,
                    verdict="pass-with-measured-gaps",
                    reason="synthetic",
                    evidence="{}",
                    now=NOW,
                    rechecks=[_EVENT],
                )

        asyncio.run(corrupt_and_add_neighbor())
        _w._print_verification_log(str(db), 40)
    finally:
        sys.setrecursionlimit(previous_limit)
    records = {r["repo"]: r for r in _scoped_records(capsys.readouterr().out)}
    assert records[REPO][column] == raw
    assert records["owner/z-next"]["rechecks"][0]["recheck"] == _EVENT["recheck"]


@pytest.mark.parametrize(
    "raw", ["not json", "[" * 10000 + "0" + "]" * 10000, '[{"results":[],"results":[]}]']
)
def test_log_preserves_unreadable_and_ambiguous_rechecks_verbatim(tmp_path, capsys, raw):
    db = tmp_path / "synthetic.db"
    _build_rechecks(db, [_EVENT])

    async def corrupt():
        async with aiosqlite.connect(db) as conn:
            await conn.execute("UPDATE pr_verifications SET rechecks = ?", (raw,))
            await conn.commit()

    asyncio.run(corrupt())
    _w._print_verification_log(str(db), 40)
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["rechecks"] == raw


@pytest.mark.parametrize("folder_name", ["ledger", "ledger with spaces", "ledger's [quoted]"])
def test_malformed_hint_preserves_the_selected_database_path(tmp_path, capsys, folder_name):
    folder = tmp_path / folder_name
    folder.mkdir()
    db = folder / "synthetic.db"
    _build_rechecks(db, [_EVENT])

    async def corrupt():
        async with aiosqlite.connect(db) as conn:
            await conn.execute("UPDATE pr_verifications SET rechecks = ?", ("not-json",))
            await conn.commit()

    asyncio.run(corrupt())
    _w._print_verification_backlog(str(db))
    line = next(
        line for line in capsys.readouterr().out.splitlines() if "malformed rechecks" in line
    )
    args = shlex.split(line.split("read it with ", 1)[1])
    assert "--db-path" in args, "inspection must retain the selected database"
    assert args[args.index("--db-path") + 1] == str(db.resolve())
    pr = int(args[args.index("--pr") + 1])
    _w._print_verification_log(args[args.index("--db-path") + 1], pr)
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["repo"] == REPO and record["pr_number"] == 40
    assert record["rechecks"] == "not-json"


def test_malformed_hint_without_a_selected_database_keeps_the_default(capsys):
    _w._print_rechecks_due([{"repo": REPO, "pr_number": 40, "malformed": True}])
    hint = capsys.readouterr().out.split("read it with ", 1)[1]
    assert shlex.split(hint) == ["--verification-log", "--pr", "40"]


def test_record_command_preserves_the_selected_database_path(tmp_path, capsys):
    folder = tmp_path / "ledger with spaces"
    folder.mkdir()
    db = folder / "synthetic.db"
    _build_rechecks(db, [_EVENT])
    _w._print_verification_backlog(str(db))
    command = next(
        line.split("record : ", 1)[1]
        for line in capsys.readouterr().out.splitlines()
        if "record : " in line
    )
    args = shlex.split(command)
    assert args[args.index("--db-path") + 1] == str(db)


def test_printed_commands_name_the_interpreter_running_the_reader(tmp_path, capsys):
    """A bare `python3` may lack aiosqlite, which pr_verification.py needs, so
    both commands the backlog prints start with this interpreter."""
    import sys

    db = tmp_path / "synthetic.db"
    _build_rechecks(db, [_EVENT])
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    recheck = next(
        line.split("record : ", 1)[1] for line in out.splitlines() if "record : " in line
    )
    close = next(
        line.split("close one: ", 1)[1] for line in out.splitlines() if "close one: " in line
    )
    assert shlex.split(recheck)[0] == sys.executable
    assert shlex.split(close)[0] == sys.executable


@pytest.mark.parametrize("bad_results", [["bad"], [None], [{}], "bad", 0])
def test_malformed_history_is_shown_once_and_later_valid_row_survives(
    tmp_path, capsys, bad_results
):
    db = tmp_path / "synthetic.db"
    _build_rechecks(db, [_EVENT])
    _build_rechecks(db, [_EVENT], pr_number=41)

    async def corrupt():
        async with aiosqlite.connect(db) as conn:
            raw = json.dumps([{**_EVENT, "results": bad_results, "done_at": None}])
            await conn.execute(
                "UPDATE pr_verifications SET rechecks = ? WHERE pr_number = 40", (raw,)
            )
            await conn.commit()

    asyncio.run(corrupt())
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert out.count("malformed rechecks or result history") == 1
    assert "owner/repo#41" in out and "recheck --pr 41 --index 0" in out


def _build_rechecks(
    db: Path, rechecks: list[dict], *, results: tuple = (), pr_number: int = 40
) -> None:
    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=pr_number, pr_title="t", merged_at=NOW, now=NOW
            )
            await crud.close_verification(
                conn,
                repo=REPO,
                pr_number=pr_number,
                verdict="pass-with-measured-gaps",
                reason="r",
                evidence="{}",
                now=NOW,
                rechecks=rechecks,
            )
            for index, result in results:
                await crud.append_recheck_result(
                    conn,
                    repo=REPO,
                    pr_number=pr_number,
                    index=index,
                    result=result,
                    note="n",
                    now=NOW,
                )

    asyncio.run(go())


def test_a_due_recheck_prints_the_exact_record_command_and_its_history(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build_rechecks(db, [_FUTURE, _PAST, _EVENT], results=((1, "failed"), (1, "failed")))
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "Rechecks due: 2" in out
    assert "recheck --pr 40 --index 1 --result-file" in out, "the stored index, exactly"
    assert "recheck --pr 40 --index 2 --result-file" in out
    assert "--index 0" not in out, "a future date is not due"
    assert "latest failed 2026-09-20, 2 attempt(s)" in out
    assert "never rechecked" in out
    assert f"recheck: {_PAST['recheck']}" in out, "the command, whole"


def test_a_finished_recheck_leaves_the_section(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build_rechecks(db, [_EVENT], results=((0, "done"),))
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "Rechecks due: 0" in out and "RECHECK" not in out


def test_the_rechecks_section_is_skipped_before_its_migration(tmp_path, capsys):
    db = tmp_path / "genesis.db"

    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud
        from tests.test_session_awareness.conftest import PR_VERIFICATION_MIGRATIONS

        async with aiosqlite.connect(str(db)) as conn:
            for mig in PR_VERIFICATION_MIGRATIONS[:2]:
                await mig.up(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=1, pr_title="t", merged_at=NOW, now=NOW
            )

    asyncio.run(go())
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "Rechecks due: skipped" in out and "pre-migration" in out
    assert "OPEN  owner/repo#1" in out, "the rest of the backlog still prints"


def test_a_malformed_rechecks_column_is_shown_not_hidden(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build_rechecks(db, [_EVENT])

    async def corrupt() -> None:
        async with aiosqlite.connect(str(db)) as conn:
            await conn.execute("UPDATE pr_verifications SET rechecks = ?", ("not json",))
            await conn.commit()

    asyncio.run(corrupt())
    _w._print_verification_backlog(str(db))
    assert "malformed rechecks or result history" in capsys.readouterr().out


def test_the_log_reader_prints_rechecks_as_STRUCTURE(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build_rechecks(db, [_EVENT])
    _w._print_verification_log(str(db), 40)
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["rechecks"][0]["recheck"] == _EVENT["recheck"], "parsed, not a string"
