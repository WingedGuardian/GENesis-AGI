"""Result CLI and preview integration on disposable migrated ledgers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from tests.test_scripts.test_pr_verification_closer import (
    EVENT_GAP,
    NOW,
    OTHER,
    REPO,
    UNDONE_GAP,
    _doc,
    _gap,
    _prv,
    _row,
    _run,
    _seed,
)
from tests.test_scripts.test_pr_verification_closer import (
    _no_real_github as _no_real_github,
)

DONE = {"repo": REPO, "result": "done", "note": "7 of 7 weekly runs present"}
FAILED = {"repo": REPO, "result": "failed", "note": "the run after the trigger is missing"}


@pytest.mark.parametrize("needle", ["pr_verification: appended", "The recheck FAILED"])
def test_reporting_failure_after_append_reports_committed(tmp_path, capsys, monkeypatch, needle):
    import builtins

    db = _closed_with_recheck(tmp_path)
    original = builtins.print

    def fail_output(*args, **kwargs):
        if args and needle in str(args[0]) and "file" not in kwargs:
            raise OSError("synthetic output failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fail_output)
    assert _recheck(tmp_path, FAILED) == 0
    assert "COMMITTED" in capsys.readouterr().err
    stored = json.loads(_row(db)["rechecks"])[0]
    assert len(stored["results"]) == 1 and stored["results"][0]["note"] == FAILED["note"]


@pytest.mark.parametrize("command", ["close", "recheck"])
def test_giant_json_integer_is_a_named_malformed_request(tmp_path, capsys, command):
    source = tmp_path / "number.json"
    source.write_text("1" * 5000)
    args = [command, "--pr", "7"]
    args += (
        ["--evidence-file", str(source)]
        if command == "close"
        else ["--index", "0", "--result-file", str(source)]
    )
    assert _prv.main(args) == 2
    assert "malformed" in capsys.readouterr().err


@pytest.mark.parametrize("pr", [-1, 0, 2**63, 10**100])
def test_recheck_pr_domain_matches_sqlite_integer_key(tmp_path, capsys, pr):
    db = _closed_with_recheck(tmp_path)
    before = _row(db)
    assert _recheck(tmp_path, DONE, pr=pr) == 2
    assert "--pr" in capsys.readouterr().err
    assert _row(db) == before


@pytest.mark.parametrize("scalar", [None, False, True, 0, 1, 2, -1, 255, 256, 1.5, "text", []])
@pytest.mark.parametrize("command", ["close", "recheck"])
def test_json_scalars_are_validated_and_never_become_exit_codes(tmp_path, capsys, scalar, command):
    source = tmp_path / "scalar.json"
    source.write_text(json.dumps(scalar))
    args = [command, "--pr", "7"]
    args += (
        ["--evidence-file", str(source)]
        if command == "close"
        else ["--index", "0", "--result-file", str(source)]
    )
    assert _prv.main(args) == 2
    assert "malformed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw",
    [b'{"repo":"owner/repo","repo":"owner/repo"}', b"\xff", b"{", b"[" * 2000 + b"0" + b"]" * 2000],
)
def test_result_loader_refuses_duplicate_utf8_json_and_depth_errors(tmp_path, raw):
    source = tmp_path / "bad.json"
    source.write_bytes(raw)
    assert _prv.main(["recheck", "--pr", "7", "--index", "0", "--result-file", str(source)]) == 2


@pytest.mark.parametrize("index", [2**31, 2**32, 2**32 + 1, 2**64])
def test_cli_and_preview_refuse_wrapping_indices_without_mutation(tmp_path, index):
    db = _closed_with_recheck(tmp_path)
    before = _row(db)
    assert _recheck(tmp_path, DONE, index=index) == 2
    assert _recheck(tmp_path, DONE, "--dry-run", index=index) == 2
    assert _row(db) == before


def test_a_closing_document_stores_its_event_gated_gap_as_a_recheck(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(gaps=[_gap("method limit"), EVENT_GAP])) == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("closed", "pass-with-measured-gaps")
    assert json.loads(row["rechecks"]) == [
        {
            "trigger": EVENT_GAP["trigger"],
            "recheck": EVENT_GAP["recheck"],
            "detail": EVENT_GAP["detail"],
            "results": [],
            "done_at": None,
        }
    ]
    assert "1 recheck(s) stored" in capsys.readouterr().out


def test_a_dry_run_lists_the_rechecks_it_would_store(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(gaps=[EVENT_GAP]), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "rechecks : 1 to store" in out and "--index 0  2026-11-02" in out
    assert _row(db)["rechecks"] is None, "nothing written"


def test_a_non_closing_outcome_stores_no_recheck(tmp_path):
    """Rechecks are owed by a DISCHARGED row; an open row is still the obligation."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(gaps=[EVENT_GAP, UNDONE_GAP])) == 0
    row = _row(db)
    assert (row["status"], row["rechecks"]) == ("open", None)


def _seed_without_rechecks_column(db: Path) -> None:
    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud
        from tests.test_session_awareness.conftest import PR_VERIFICATION_MIGRATIONS

        async with aiosqlite.connect(str(db)) as conn:
            for mig in PR_VERIFICATION_MIGRATIONS[:2]:
                await mig.up(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=7, pr_title="t", merged_at=NOW, now=NOW
            )

    asyncio.run(go())


@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_before_the_rechecks_migration_a_close_carrying_rechecks_is_REFUSED(
    tmp_path, capsys, extra
):
    """Closing anyway would discharge the row with its owed measurements written
    nowhere a reader lists them — the loss this column exists to prevent."""
    db = tmp_path / "genesis.db"
    _seed_without_rechecks_column(db)
    assert _run(tmp_path, _doc(gaps=[EVENT_GAP]), *extra) == 1
    err = capsys.readouterr().err
    assert "rechecks" in err and "not present yet" in err and "Nothing" in err
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("open", None), "nothing written"


def test_before_the_rechecks_migration_a_close_with_no_event_gap_still_lands(tmp_path):
    db = tmp_path / "genesis.db"
    _seed_without_rechecks_column(db)
    assert _run(tmp_path, _doc(gaps=[_gap("method limit")])) == 0
    assert _row(db)["status"] == "closed"


def _closed_with_recheck(tmp_path: Path) -> Path:
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(gaps=[EVENT_GAP])) == 0
    return db


def _recheck(tmp_path: Path, result: dict, *extra: str, pr: int = 7, index: int = 0) -> int:
    rf = tmp_path / "result.json"
    rf.write_text(json.dumps(result))
    return _prv.main(
        [
            "recheck",
            "--pr",
            str(pr),
            "--index",
            str(index),
            "--result-file",
            str(rf),
            "--db-path",
            str(tmp_path / "genesis.db"),
            *extra,
        ]
    )


def test_a_done_recheck_is_recorded_and_the_row_stays_closed(tmp_path, capsys):
    db = _closed_with_recheck(tmp_path)
    before = _row(db)
    assert _recheck(tmp_path, DONE) == 0
    after = _row(db)
    for col in ("status", "verdict", "closed_at", "closed_reason", "evidence"):
        assert after[col] == before[col], f"{col} changed — recheck must never reopen"
    (recheck,) = json.loads(after["rechecks"])
    assert recheck["done_at"] is not None
    assert recheck["results"][0]["note"] == DONE["note"]
    assert "stays CLOSED" in capsys.readouterr().out


def test_a_failed_recheck_says_bring_it_to_the_user_and_files_nothing(tmp_path, capsys):
    db = _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, FAILED) == 0
    out = capsys.readouterr().out
    assert "bring it to the user as fail-intent" in out and "Nothing was filed" in out
    (recheck,) = json.loads(_row(db)["rechecks"])
    assert recheck["done_at"] is None and recheck["results"][0]["result"] == "failed"
    assert _row(db)["status"] == "closed"


def test_a_finished_recheck_refuses_a_second_result(tmp_path, capsys):
    _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, DONE) == 0
    capsys.readouterr()
    assert _recheck(tmp_path, FAILED) == 1
    assert "already done" in capsys.readouterr().err


@pytest.mark.parametrize(("index", "code"), [(1, 2), (-1, 2)])
def test_an_index_outside_the_stored_rechecks_is_refused(tmp_path, capsys, index, code):
    _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, DONE, index=index) == code
    assert "--index" in capsys.readouterr().err


def test_a_recheck_on_a_row_with_no_rechecks_or_no_closed_row_is_refused(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _recheck(tmp_path, DONE) == 1, "an OPEN row has no rechecks to record against"
    assert "no CLOSED row" in capsys.readouterr().err
    assert _run(tmp_path, _doc()) == 0  # closed, no event-gated gap -> rechecks NULL
    assert _recheck(tmp_path, DONE) == 1
    assert "stores no rechecks" in capsys.readouterr().err
    assert _recheck(tmp_path, {**DONE, "repo": OTHER}) == 1, "the file names the row"


@pytest.mark.parametrize(
    "bad",
    [
        {"result": "done", "note": "n"},
        {"repo": REPO, "result": "pass", "note": "n"},
        {"repo": REPO, "result": "done", "note": ""},
        {"repo": REPO, "result": "done", "note": "n", "extra": 1},
    ],
)
def test_a_malformed_result_file_is_refused_by_field(tmp_path, capsys, bad):
    _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, bad) == 2
    assert "result." in capsys.readouterr().err


def test_a_recheck_dry_run_writes_nothing_and_still_checks_the_target(tmp_path, capsys):
    db = _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, DONE, "--dry-run") == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert json.loads(_row(db)["rechecks"])[0]["results"] == []
    assert _recheck(tmp_path, DONE, "--dry-run", index=3) == 2


def test_recheck_refuses_before_the_rechecks_migration(tmp_path, capsys):
    _seed_without_rechecks_column(tmp_path / "genesis.db")
    assert _recheck(tmp_path, DONE) == 1
    assert "not present yet" in capsys.readouterr().err


def test_a_dry_run_of_a_failed_result_does_not_claim_it_was_recorded(tmp_path, capsys):
    db = _closed_with_recheck(tmp_path)
    assert _recheck(tmp_path, FAILED, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Nothing was filed" not in out and "The recheck FAILED" not in out
    assert json.loads(_row(db)["rechecks"])[0]["results"] == []


def _set_rechecks(db: Path, raw: str) -> None:
    async def go() -> None:
        async with aiosqlite.connect(str(db)) as conn:
            await conn.execute("UPDATE pr_verifications SET rechecks = ?", (raw,))
            await conn.commit()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "is not valid JSON"),
        ('[{"trigger": "t", "recheck": "r", "detail": "d", "results": 5}]', "stored shape"),
        ('["a string"]', "stored shape"),
        ("[]", "stores no rechecks"),
    ],
)
def test_a_recheck_against_an_unreadable_column_names_what_is_wrong(tmp_path, capsys, raw, message):
    _closed_with_recheck(tmp_path)
    _set_rechecks(tmp_path / "genesis.db", raw)
    assert _recheck(tmp_path, DONE) == 1
    assert message in capsys.readouterr().err
