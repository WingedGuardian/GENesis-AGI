"""Inbox coverage report: per-URL classification order, corpus scoping,
read-only guarantees and rendered denominators."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "inbox_coverage_report", _SCRIPTS_DIR / "inbox_coverage_report.py"
)
_rep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _rep
_spec.loader.exec_module(_rep)

from genesis.db.crud.inbox_items import serialize_batch_items  # noqa: E402

URL = "https://example.com/a"

_COLUMNS = """
CREATE TABLE inbox_items (
    id INTEGER PRIMARY KEY,
    file_path TEXT,
    status TEXT,
    error_message TEXT,
    response_path TEXT,
    processed_at TEXT,
    created_at TEXT,
    batch_items TEXT
)
"""


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(_COLUMNS)


def _insert(
    db: Path,
    file_path: Path,
    *,
    status: str = "completed",
    batch_items: object = None,
    error_message: str | None = None,
    response_path: str | None = None,
    processed_at: str = "2026-10-01T00:00:00",
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO inbox_items (file_path, status, error_message,"
            " response_path, processed_at, created_at, batch_items)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(file_path),
                status,
                error_message,
                response_path,
                processed_at,
                processed_at,
                batch_items,
            ),
        )


def _v2(text: str) -> str:
    return serialize_batch_items([text])


def _run(
    tmp_path: Path, *, note_text: str | None = None, extra_notes: dict[str, str] | None = None
) -> tuple[Path, Path, Path, str]:
    """Temp inbox + DB; returns (inbox, db, out, rendered_markdown)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    if note_text is not None:
        note.write_text(note_text)
    for rel, text in (extra_notes or {}).items():
        p = inbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    db = tmp_path / "genesis.db"
    _make_db(db)
    out = tmp_path / "out.md"
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)])
    assert rc == 0
    return inbox, db, note, out.read_text()


def test_evaluated_with_present_covered_response(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"look at {URL}\n")
    response = tmp_path / "note.genesis.md"
    response.write_text(f"**Source:** {URL}\nsummary\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(db, note, batch_items=_v2(f"look at {URL}"), response_path=str(response))
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | evaluated | present | yes |  |" in out.read_text()


def test_evaluated_response_missing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(
        db,
        note,
        batch_items=_v2(URL),
        response_path=str(tmp_path / "gone.genesis.md"),
    )
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | evaluated | missing | unknown |  |" in out.read_text()


def test_evaluated_not_covered(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    response = tmp_path / "r.genesis.md"
    response.write_text("**Source:** https://example.org/other\nno mention\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(db, note, batch_items=_v2(URL), response_path=str(response))
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | evaluated | present | no |  |" in out.read_text()


def test_tracking_params_normalized(tmp_path: Path) -> None:
    dirty = f"{URL}?utm_source=x"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{dirty}\n")
    response = tmp_path / "r.genesis.md"
    response.write_text(f"**Source:** {URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(db, note, batch_items=_v2(URL), response_path=str(response))
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    md = out.read_text()
    assert f"| {URL} | evaluated | present | yes |  |" in md
    assert dirty not in md


def test_newest_completed_row_wins(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    newer_response = tmp_path / "new.genesis.md"
    newer_response.write_text(f"**Source:** {URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(
        db,
        note,
        batch_items=_v2(URL),
        response_path=str(tmp_path / "old.genesis.md"),
        processed_at="2026-10-01T00:00:00",
    )
    _insert(
        db,
        note,
        batch_items=_v2(URL),
        response_path=str(newer_response),
        processed_at="2026-10-02T00:00:00",
    )
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | evaluated | present | yes |  |" in out.read_text()


def test_in_flight(tmp_path: Path) -> None:
    inbox, db, note, md = _run(tmp_path, note_text=f"{URL}\n")
    _insert(db, note, status="pending", batch_items=_v2(URL))
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | in_flight |  |  |  |" in out.read_text()


def test_failed_reason_prefix(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(
        db,
        note,
        status="failed",
        batch_items=_v2(URL),
        error_message="partial_url_failure: uncovered",
    )
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert (
        f"| {URL} | failed |  |  | partial_url_failure |" in out.read_text()
    )


def test_legacy_row_unattributable(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    # Legacy plain-text batch_items: completed but carries no item boundary.
    _insert(db, note, status="completed", batch_items=f"saw {URL}")
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | unattributable |  |  |  |" in out.read_text()


def test_never_evaluated(tmp_path: Path) -> None:
    _, _, _, md = _run(tmp_path, note_text=f"{URL}\n")
    assert f"| {URL} | never |  |  |  |" in md


def test_responses_and_dotdirs_excluded(tmp_path: Path) -> None:
    _, _, _, md = _run(
        tmp_path,
        extra_notes={
            "a.genesis.md": f"**Source:** {URL}\n",
            ".trash/gone.md": f"{URL}\n",
            ".hidden.md": f"{URL}\n",
        },
    )
    assert "| url |" not in md
    assert "Notes with no URLs: 0" in md


def test_recursive_notes(tmp_path: Path) -> None:
    _, _, _, md = _run(tmp_path, extra_notes={"sub/deep.md": f"{URL}\n"})
    assert "## sub/deep.md" in md
    assert f"| {URL} | never |  |  |  |" in md


def test_db_opened_read_only(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    before = db.read_bytes()
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert db.read_bytes() == before
    conn = _rep.connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO inbox_items (file_path) VALUES ('x')")
    conn.close()


def test_missing_inbox_exits_2(tmp_path: Path) -> None:
    db = tmp_path / "genesis.db"
    _make_db(db)
    rc = _rep.main(
        [
            "--inbox",
            str(tmp_path / "nope"),
            "--db",
            str(db),
            "--out",
            str(tmp_path / "out.md"),
        ]
    )
    assert rc == 2


def test_missing_db_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    rc = _rep.main(
        [
            "--inbox",
            str(inbox),
            "--db",
            str(tmp_path / "nope.db"),
            "--out",
            str(tmp_path / "out.md"),
        ]
    )
    assert rc == 2


def test_out_inside_inbox_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    rc = _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(note)]
    )
    assert rc == 2
    assert note.read_text() == f"{URL}\n"


def test_response_file_out_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_text(f"{URL}\n")
    response = tmp_path / "note.genesis.md"
    response.write_text(f"**Source:** {URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    rc = _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(response)]
    )
    assert rc == 2
    assert response.read_text() == f"**Source:** {URL}\n"


def test_tracked_url_coverage_uses_original(tmp_path: Path) -> None:
    tracked = f"{URL}?utm_source=x"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{tracked}\n")
    # The response cited exactly what the item carried — the live gate calls
    # that covered, and the report must agree.
    response = tmp_path / "r.genesis.md"
    response.write_text(f"**Source:** {tracked}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(db, note, batch_items=_v2(tracked), response_path=str(response))
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | evaluated | present | yes |  |" in out.read_text()


def test_out_db_path_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    before = db.read_bytes()
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(db)])
    assert rc == 2
    assert db.read_bytes() == before


def test_non_utf8_note_tolerated(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_bytes(f"win1252 \x92quote {URL}\n".encode())
    db = tmp_path / "genesis.db"
    _make_db(db)
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    assert f"| {URL} | never |  |  |  |" in out.read_text()


def test_reason_cell_escaped(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(f"{URL}\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(
        db,
        note,
        status="failed",
        batch_items=_v2(URL),
        error_message="a|b: detail",
    )
    out = tmp_path / "out.md"
    assert _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--out", str(out)]
    ) == 0
    md = out.read_text()
    assert f"| {URL} | failed |  |  | a\\|b |" in md


def test_render_has_denominators(tmp_path: Path) -> None:
    _, _, _, md = _run(tmp_path, note_text=f"{URL}\nhttps://example.org/b\n")
    assert (
        "Totals: 2 URLs across 1 notes — 0 evaluated, 0 in flight, "
        "0 failed, 0 unattributable, 2 never" in md
    )
