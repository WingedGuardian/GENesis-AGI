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
    assert "#1 " in out or "#1\n" in out or "#1" in out
    assert "no closed rows" not in out
    assert "pass-mechanical" in out


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


def test_the_verdict_and_evidence_are_surfaced(tmp_path, capsys):
    """The D3 obligation: the reader must show what the writer wrote, or the
    evidence column is write-only and a retention timer deletes it unread."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    _w._print_verification_log(str(db), 1)
    out = capsys.readouterr().out
    assert "pass-mechanical" in out
    assert "evidence:" in out and "bytes" in out
    assert "reason  :" in out


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


def test_a_scoped_read_prints_the_evidence_DOCUMENT(tmp_path, capsys):
    """A byte count proves the write happened and says nothing about what was
    concluded, which left the column write-only for the purpose it exists for. A
    scoped read is someone asking what was decided, so the document is shown."""
    db = tmp_path / "genesis.db"
    _build(db, closed=1)
    _w._print_verification_log(str(db), 1)
    out = capsys.readouterr().out
    assert '"x": 1' in out or '"x":1' in out, "the document's contents must appear"


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


def test_a_clipped_document_SAYS_it_was_clipped(tmp_path, capsys):
    """A silently truncated evidence record still looks complete — the same failure the
    writer refuses an over-cap document to avoid."""
    db = tmp_path / "genesis.db"

    async def go() -> None:
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
                evidence="z" * (_w._EVIDENCE_SHOW + 500),
                now=NOW,
            )

    asyncio.run(go())
    _w._print_verification_log(str(db), 5)
    out = capsys.readouterr().out
    assert "CLIPPED at" in out
    assert str(_w._EVIDENCE_SHOW + 500) in out, "the real size must be stated"


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


def test_a_scoped_read_renders_a_PARKED_row_as_OPEN(tmp_path, capsys):
    """A mutation sweep measured this unpinned: rendering an open row as CLOSED left
    every test green. It is the one line that tells a reader the obligation is NOT
    discharged, so printing the wrong word there is the worst thing this reader can
    do — a parked fail-intent would read as settled."""
    db = tmp_path / "genesis.db"
    _build(db, open_rows=1, parked=True)
    _w._print_verification_log(str(db), 1)
    out = capsys.readouterr().out
    assert out.startswith("OPEN") or "\nOPEN " in out
    assert "CLOSED" not in out, "an undischarged obligation must never read as closed"
    assert "cannot-verify" in out
    assert "needs another install entirely" in out
    assert "attempts: 1" in out


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
