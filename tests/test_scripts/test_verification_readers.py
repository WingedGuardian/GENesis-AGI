"""The two ``pr_verifications`` CLI readers on ``scripts/repo_pulse_worker.py``.

These had NO tests — a review measured `grep` for either function name in
``tests/`` returning nothing, and that is where the discipline broke: the closed-row
reader paged 500 rows and filtered in Python, so with 511 closed rows it reported
"no closed rows for PR #1" about a row that was closed ``pass-mechanical``, and
blamed an empty or pre-migration table for it. The sibling reader's own docstring
forbids that shape a hundred lines away.

What is pinned here: the exit-code-always-0 contract this module documents; that a
scoped read filters in SQL rather than over a page; that a capped read SAYS it was
capped; that a NULL verdict is stated rather than blanked; and that every "nothing
here" state is distinguishable from every other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from tests.conftest import private_module
from tests.test_session_awareness.conftest import build_pr_verifications

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "repo_pulse_worker.py"
_w = private_module("repo_pulse_worker_cli_under_test", _SCRIPT)

REPO = "owner/repo"
NOW = "2026-09-20T11:00:00+00:00"


def _build(db: Path, *, closed: int = 0, open_rows: int = 0, parked: bool = False) -> None:
    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            n = 0
            for i in range(closed):
                n += 1
                await crud.open_verification(
                    conn,
                    repo=REPO,
                    pr_number=n,
                    pr_title="t",
                    merged_at=f"2026-09-{(i % 27) + 1:02d}T00:00:00Z",
                    now=NOW,
                )
                await crud.close_verification(
                    conn,
                    repo=REPO,
                    pr_number=n,
                    verdict="pass-mechanical",
                    reason="r",
                    evidence='{"x":1}',
                    now=f"2026-09-{(i % 27) + 1:02d}T01:00:00+00:00",
                )
            for i in range(open_rows):
                n += 1
                await crud.open_verification(
                    conn,
                    repo=REPO,
                    pr_number=n,
                    pr_title="t",
                    merged_at=f"2026-09-{(i % 27) + 1:02d}T00:00:00Z",
                    now=NOW,
                )
                if parked and i == 0:
                    await crud.record_attempt(
                        conn,
                        repo=REPO,
                        pr_number=n,
                        verdict="cannot-verify",
                        note="needs another install entirely",
                        now=NOW,
                    )

    asyncio.run(go())


# ── the exit-code contract ───────────────────────────────────────────────


@pytest.mark.parametrize("missing", [True, False])
def test_both_readers_return_none_so_the_always_zero_contract_holds(tmp_path, missing):
    """This module documents "exit code is always 0 unless argument parsing fails",
    because a detached hook spawns it and nothing reads its status. A reader that
    returned a code — or raised — would break that for every caller."""
    db = tmp_path / "genesis.db"
    if not missing:
        _build(db, closed=1, open_rows=1)
    target = str(db) if not missing else str(tmp_path / "absent.db")
    assert _w._print_verification_backlog(target) is None
    assert _w._print_verification_log(target, None) is None


# ── the closed-row reader ────────────────────────────────────────────────


def test_a_scoped_read_finds_a_row_beyond_the_page(tmp_path, capsys):
    """THE regression. Filtering in Python over a capped page made a row past the
    cap report as absent, with 'table empty or pre-migration' as the stated cause."""
    db = tmp_path / "genesis.db"
    _build(db, closed=_w._LOG_LIMIT + 11)
    _w._print_verification_log(str(db), 1)  # PR 1 is the OLDEST closure
    out = capsys.readouterr().out
    assert "no closed rows" not in out
    # The ROW, not the footer: "#1" alone was satisfied by the footer's "PR #1".
    (record,) = _scoped_records(out)
    assert record["pr_number"] == 1
    assert record["verdict"] == "pass-mechanical"


def test_a_capped_read_says_it_was_capped(tmp_path, capsys):
    """A listing whose length equals its cap is a truncated read; printing the count
    alone invites a reader to take it for a total."""
    db = tmp_path / "genesis.db"
    _build(db, closed=_w._LOG_LIMIT + 5)
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert "CAPPED read" in out
    assert "not a total" in out


def test_an_uncapped_read_does_not_claim_truncation(tmp_path, capsys):
    """The other direction — otherwise the disclosure is noise that gets ignored."""
    db = tmp_path / "genesis.db"
    _build(db, closed=3)
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert "CAPPED read" not in out
    assert "3 closed row(s) shown" in out


def test_a_null_verdict_is_STATED_not_blanked(tmp_path, capsys):
    """A docs-exempt closure has no verdict, and that is a fact about the row —
    printing an empty column would read as 'a validator concluded nothing'."""
    db = tmp_path / "genesis.db"

    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn,
                repo=REPO,
                pr_number=9,
                pr_title="docs: x",
                merged_at=NOW,
                now=NOW,
                closed_reason="docs-only by path rule — deterministic exemption",
            )

    asyncio.run(go())
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert "auto-exempt by path" in out
    assert "evidence: none recorded" in out


def test_no_matching_closed_row_is_distinguishable_from_an_empty_table(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    _w._print_verification_log(str(db), 4242)
    out = capsys.readouterr().out
    assert "no rows for PR #4242" in out
    # The cause must not be misstated, and it differs by mode now: a SCOPED read covers
    # open rows, so "nothing has been discharged" would be wrong for it — an
    # undischarged row is precisely what it would have shown.
    assert "no obligation was ever opened for it" in out
    assert "covers OPEN rows too" in out


def test_an_absent_database_is_reported_as_itself(tmp_path, capsys):
    _w._print_verification_log(str(tmp_path / "nope.db"), None)
    assert "no database at" in capsys.readouterr().out


def test_a_pre_migration_database_does_not_crash_the_reader(tmp_path, capsys):
    """The verdict columns may not exist yet. ``list_closed`` does ``SELECT *``, so
    the key is simply ABSENT from the row dict rather than None — the reader must
    tolerate that instead of raising KeyError."""
    db = tmp_path / "genesis.db"

    async def old_only() -> None:
        from genesis.db.crud import pr_verifications as crud
        from tests.test_session_awareness.conftest import PR_VERIFICATION_MIGRATIONS

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await PR_VERIFICATION_MIGRATIONS[0].up(conn)
            await conn.commit()
            await crud.open_verification(
                conn,
                repo=REPO,
                pr_number=3,
                pr_title="docs",
                merged_at=NOW,
                now=NOW,
                closed_reason="docs-only by path rule",
            )

    asyncio.run(old_only())
    assert _w._print_verification_log(str(db), None) is None
    out = capsys.readouterr().out
    assert "auto-exempt by path" in out, "an absent column must read as no verdict"


# ── the backlog reader's parked annotation ───────────────────────────────


def test_a_parked_row_is_annotated_inline_for_the_next_validator(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _build(db, open_rows=3, parked=True)
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "ATTEMPTED 1x  cannot-verify" in out
    assert "needs another install entirely" in out


def test_an_unattempted_row_carries_no_annotation(tmp_path, capsys):
    """The other direction — otherwise the annotation is unconditional and says
    nothing."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=2, parked=False)
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert "ATTEMPTED" not in out


def test_the_backlog_advertises_the_close_command(tmp_path, capsys):
    """The reader is where a validator lands first; it used to point at raw SQL."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=1)
    _w._print_verification_backlog(str(db))
    assert "pr_verification.py close" in capsys.readouterr().out


# ── the evidence column is READ, not just counted (round 1, cause C) ──────


def test_an_unscoped_read_points_at_the_document_instead_of_printing_it(tmp_path, capsys):
    """The other direction. An unscoped listing can carry 500 rows, and printing every
    document would bury the census this mode exists to give — so it names the command
    that shows one."""
    db = tmp_path / "genesis.db"
    _build(db, closed=3)
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert "--verification-log --pr" in out
    # The fixture stores the COMPACT spelling, so asserting the spaced one absent was
    # vacuous — it could never have appeared even if the document were printed. An
    # adversarial audit caught it; the control now names what is actually stored.
    assert '{"x":1}' not in out


# ── a flag that is silently ignored misreports the run ───────────────────


def test_pr_without_the_log_flag_is_an_argument_error(tmp_path, monkeypatch):
    """Accepted-and-ignored, --pr reads as a filter that was applied: a full pulse run
    would touch every PR while the operator believed they had narrowed it. parser.error
    exits 2, the one nonzero this module's always-exit-0 contract already allows."""
    monkeypatch.setattr(
        "sys.argv", ["repo_pulse_worker.py", "--pr", "7", "--db-path", str(tmp_path / "x.db")]
    )
    with pytest.raises(SystemExit) as exc:
        _w.main()
    assert exc.value.code == 2


def test_pr_WITH_the_log_flag_is_accepted(tmp_path, monkeypatch):
    """The control arm. Without it the guard could reject --pr unconditionally and
    every assertion above would still hold."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    monkeypatch.setattr(
        "sys.argv",
        ["repo_pulse_worker.py", "--verification-log", "--pr", "1", "--db-path", str(db)],
    )
    assert _w.main() is None


def test_a_scoped_read_COUNTS_a_parked_row_as_open(tmp_path, capsys):
    """Round-2 finding: the footer said '1 closed row(s) shown' directly under a line
    reading OPEN. A reader gets two answers about whether the obligation is pending."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=1, parked=True)
    _w._print_verification_log(str(db), 1)
    out = capsys.readouterr().out
    assert "1 open, 0 closed" in out
    assert "closed row(s) shown" not in out, "the closed-only footer is for unscoped reads"
    assert "CAPPED read" not in out


# ── round 3: the scoped read is the RECORD, unclipped and unrelabelled ─────


def _scoped_records(out: str) -> list[dict]:
    """Parse the JSON objects a scoped read prints (everything but the footer)."""
    import json

    body = out[: out.rindex("pr_verifications:")]
    decoder, pos, records = json.JSONDecoder(), 0, []
    while True:
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            return records
        obj, pos = decoder.raw_decode(body, pos)
        records.append(obj)


def test_a_scoped_read_prints_the_row_itself_with_the_evidence_as_STRUCTURE(tmp_path, capsys):
    """The formatted version of this path drew defects in two review rounds; the
    record has no formatting to get wrong. Evidence comes back as an object, not a
    string of escaped JSON."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    _w._print_verification_log(str(db), 1)
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["status"] == "closed"
    assert record["verdict"] == "pass-mechanical"
    assert record["closed_reason"] == "r"
    assert record["evidence"] == {"x": 1}


def test_a_scoped_read_shows_a_PARKED_row_as_OPEN_with_its_attempt(tmp_path, capsys):
    """An undischarged obligation must never read as closed; a mutation that printed
    CLOSED here once left every test green."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=1, parked=True)
    _w._print_verification_log(str(db), 1)
    out = capsys.readouterr().out
    (record,) = _scoped_records(out)
    assert record["status"] == "open"
    assert record["verdict"] == "cannot-verify"
    assert record["last_attempt_note"] == "needs another install entirely"
    assert record["attempt_count"] == 1
    assert "1 open, 0 closed" in out


def test_a_scoped_read_never_clips_a_large_document(tmp_path, capsys):
    """The scoped read is where fidelity matters, so nothing is cut there at all."""
    db = tmp_path / "genesis.db"
    big = {"claims": ["z" * 50_000]}

    async def go() -> None:
        import json

        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=5, pr_title="t", merged_at=NOW, now=NOW
            )
            await crud.close_verification(
                conn,
                repo=REPO,
                pr_number=5,
                verdict="pass-mechanical",
                reason="r",
                evidence=json.dumps(big),
                now=NOW,
            )

    asyncio.run(go())
    _w._print_verification_log(str(db), 5)
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["evidence"] == big, "every character, structurally intact"


def test_a_scoped_read_keeps_a_non_JSON_evidence_string_verbatim(tmp_path, capsys):
    db = tmp_path / "genesis.db"

    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=6, pr_title="t", merged_at=NOW, now=NOW
            )
            await crud.close_verification(
                conn,
                repo=REPO,
                pr_number=6,
                verdict="pass-mechanical",
                reason="r",
                evidence="not json {",
                now=NOW,
            )

    asyncio.run(go())
    assert _w._print_verification_log(str(db), 6) is None, "the always-exit-0 contract"
    (record,) = _scoped_records(capsys.readouterr().out)
    assert record["evidence"] == "not json {"


# ── a census clip SAYS it clipped, and says where the rest is ────────────


def test_the_backlog_note_clip_is_DECLARED(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    long_note = "precondition: " + "x" * 300

    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=REPO, pr_number=8, pr_title="t", merged_at=NOW, now=NOW
            )
            await crud.record_attempt(
                conn, repo=REPO, pr_number=8, verdict="cannot-verify", note=long_note, now=NOW
            )

    asyncio.run(go())
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert f"(+{len(long_note) - 120} chars — --verification-log --pr 8 shows it whole)" in out


def test_a_short_note_carries_no_clip_marker(tmp_path, capsys):
    """The other direction, or the marker is unconditional noise."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=1, parked=True)
    _w._print_verification_backlog(str(db))
    assert "chars —" not in capsys.readouterr().out


def test_the_backlog_cap_notice_describes_BACKLOG_order_not_age(tmp_path, capsys, monkeypatch):
    """list_open puts never-attempted rows before parked ones, so 'the oldest N' was
    false the moment a parked row existed."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=3, parked=True)
    from genesis.db.crud import pr_verifications as crud

    real = crud.list_open

    async def capped(conn, *a, **k):
        return (await real(conn, *a, **k))[:1]

    monkeypatch.setattr(crud, "list_open", capped)
    _w._print_verification_backlog(str(db))
    out = capsys.readouterr().out
    assert "in backlog order" in out
    assert "oldest of" not in out


def test_the_log_reader_keeps_the_exit_0_contract_on_a_CORRUPT_database(tmp_path, capsys):
    """MEASURED: a file that is not a database raised straight through this reader —
    exit 1 and a traceback, against the module's always-exit-0 contract. An unreadable
    database is its own state, reported as itself."""
    db = tmp_path / "genesis.db"
    db.write_bytes(b"this is not a sqlite database at all, just bytes" * 4)
    assert _w._print_verification_log(str(db), None) is None
    assert _w._print_verification_log(str(db), 1) is None
    out = capsys.readouterr().out
    assert out.count("database unreadable") == 2


def test_the_BACKLOG_reader_keeps_the_exit_0_contract_on_a_CORRUPT_database(tmp_path, capsys):
    """Issue #2603: the backlog reader had the log reader's defect — a traceback and
    exit 1 on a file that is not a database."""
    db = tmp_path / "genesis.db"
    db.write_bytes(b"this is not a sqlite database at all, just bytes" * 4)
    assert _w._print_verification_backlog(str(db)) is None
    out = capsys.readouterr().out
    assert "database unreadable" in out
    assert "no rows" not in out, "unreadable is its own state, not an empty table"


# ── the unscoped census: the real verdict, and a declared reason clip ──────


def _close_one(db: Path, *, pr_number: int, reason: str, verdict: str = "pass-with-measured-gaps"):
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
                verdict=verdict,
                reason=reason,
                evidence='{"x":1}',
                now=NOW,
            )

    asyncio.run(go())


def test_the_census_prints_the_verdict_the_row_CARRIES(tmp_path, capsys):
    """Lost with the round-3 test rewrite; a mutation printing a constant survived."""
    db = tmp_path / "genesis.db"
    _close_one(db, pr_number=4, reason="r")
    _w._print_verification_log(str(db), None)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("CLOSED")]
    assert len(lines) == 1
    assert lines[0].endswith("pass-with-measured-gaps")
    assert "owner/repo#4" in lines[0]


def test_the_census_reason_clip_is_DECLARED(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    reason = "PASS-WITH-MEASURED-GAPS — gaps: " + "y" * 400
    _close_one(db, pr_number=11, reason=reason)
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert f"(+{len(reason) - 160} chars — --verification-log --pr 11 shows it whole)" in out
    assert reason not in out, "the census line is the clipped one"


def test_a_short_census_reason_is_printed_whole_and_unmarked(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _close_one(db, pr_number=12, reason="short reason")
    _w._print_verification_log(str(db), None)
    out = capsys.readouterr().out
    assert "reason  : short reason\n" in out
    assert "shows it whole" not in out, "no clip marker on a reason that fits"


def test_the_two_reader_modes_are_MUTUALLY_EXCLUSIVE(tmp_path, monkeypatch):
    """Codex round 3: both flags together silently ran the backlog and dropped the
    requested log and its --pr scope."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo_pulse_worker.py",
            "--verification-backlog",
            "--verification-log",
            "--pr",
            "1",
            "--db-path",
            str(tmp_path / "x.db"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        _w.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", [str(2**63), "0", "-4", "seven"])
def test_an_unbindable_pr_is_an_ARGUMENT_error_not_an_overflow(tmp_path, monkeypatch, bad):
    """Codex round 3: 2**63 reached the driver and raised OverflowError mid-read,
    outside both the argument-error exit and the sqlite3.Error handler."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    monkeypatch.setattr(
        "sys.argv",
        ["repo_pulse_worker.py", "--verification-log", "--pr", bad, "--db-path", str(db)],
    )
    with pytest.raises(SystemExit) as exc:
        _w.main()
    assert exc.value.code == 2


def test_the_largest_bindable_pr_is_accepted(tmp_path, monkeypatch, capsys):
    """The control arm: the bound is the driver's, not an arbitrary smaller one."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo_pulse_worker.py",
            "--verification-log",
            "--pr",
            str(2**63 - 1),
            "--db-path",
            str(db),
        ],
    )
    assert _w.main() is None
    assert "no rows for PR" in capsys.readouterr().out
