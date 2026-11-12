"""Inbox coverage report: per-URL classification order, corpus scoping,
read-only guarantees and rendered denominators."""

from __future__ import annotations

import importlib.util
import os
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    assert f"| {URL} | evaluated | missing | unknown |  |" in out.read_text()


def test_report_keeps_literal_and_wrapped_contexts_separate(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    content = f"[docs]({URL})\n{URL})"
    note.write_text(content)
    response = tmp_path / "response.genesis.md"
    response.write_text(f"**Source:** <{URL}>\n")
    db = tmp_path / "genesis.db"
    _make_db(db)
    _insert(db, note, batch_items=_v2(content), response_path=str(response))
    out = tmp_path / "out.md"
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    assert f"| {URL} | evaluated | present | yes |  |" in out.read_text()
    assert f"| {URL}) | evaluated | present | no |  |" in out.read_text()


@pytest.mark.parametrize(
    ("content", "citation", "expected"),
    [
        (f'"{URL}",', URL, "yes"),
        (f"{URL}\nhttps://example.org/uncited", URL, "yes"),
        (f"{URL}?utm_source=one\n{URL}?utm_source=two", f"{URL}?utm_source=one", "no"),
        (
            f"{URL}?utm_source=one\n{URL}?utm_source=two",
            f"{URL}?utm_source=one>\n**Source:** <{URL}?utm_source=two",
            "yes",
        ),
    ],
)
def test_report_checks_all_and_only_associated_occurrences(content, citation, expected):
    decoded = _rep._row_item_urls(_v2(content))
    assert decoded is not None
    row = _rep.RowInput(
        "completed",
        None,
        "fixture.genesis.md",
        "2026-01-01",
        1,
        decoded[0],
        decoded[1],
    )
    result = _rep._classify_url(URL, [row], False, lambda _: f"**Source:** <{citation}>")
    assert result["covered"] == expected


def test_report_missing_occurrence_context_is_unknown():
    row = _rep.RowInput("completed", None, "fixture.genesis.md", "2026-01-01", 1, [URL])
    result = _rep._classify_url(URL, [row], False, lambda _: f"**Source:** <{URL}>")
    assert result["covered"] == "unknown"


@pytest.mark.parametrize("width", [1, 2, 3])
def test_report_rejects_terminal_code_trim_after_an_internal_matching_run(width):
    run = "`" * width
    shortened = URL + run + "literal"
    raw = shortened + run
    decoded = _rep._row_item_urls(_v2(run + raw))
    assert decoded is not None and decoded[0] == [raw]
    row = _rep.RowInput("completed", None, "fixture.genesis.md", "2026-01-01", 1, *decoded)
    result = _rep._classify_url(raw, [row], False, lambda _: f"**Source:** <{shortened}>")
    assert result["covered"] == "no"
    result = _rep._classify_url(raw, [row], False, lambda _: f"**Source:** <{raw}>")
    assert result["covered"] == "yes"


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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    assert f"| {URL} | evaluated | present | yes |  |" in out.read_text()


def test_in_flight(tmp_path: Path) -> None:
    inbox, db, note, md = _run(tmp_path, note_text=f"{URL}\n")
    _insert(db, note, status="pending", batch_items=_v2(URL))
    out = tmp_path / "out.md"
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    assert f"| {URL} | failed |  |  | partial_url_failure |" in out.read_text()


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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(note)])
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
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(response)])
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
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
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    md = out.read_text()
    assert f"| {URL} | failed |  |  | a\\|b |" in md


def test_out_symlink_inside_inbox_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_text(f"{URL}\n")
    target = tmp_path / "outside.md"
    target.write_text("precious\n")
    link = inbox / "link.md"
    link.symlink_to(target)
    db = tmp_path / "genesis.db"
    _make_db(db)
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(link)])
    assert rc == 2
    assert target.read_text() == "precious\n"


def test_out_naming_symlinked_note_target_exits_2(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    target = tmp_path / "real.md"
    target.write_text(f"{URL}\n")
    (inbox / "note.md").symlink_to(target)
    db = tmp_path / "genesis.db"
    _make_db(db)
    rc = _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(target)])
    assert rc == 2
    assert target.read_text() == f"{URL}\n"


def test_render_has_denominators(tmp_path: Path) -> None:
    _, _, _, md = _run(tmp_path, note_text=f"{URL}\nhttps://example.org/b\n")
    assert (
        "Totals: 2 URLs across 1 notes — 0 evaluated, 0 in flight, "
        "0 failed, 0 unattributable, 2 never" in md
    )


def test_existing_output_directory_uses_generated_filename(tmp_path, capsys):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    destination = tmp_path / "reports"
    destination.mkdir()
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(destination)]) == 0
    report = Path(capsys.readouterr().out.splitlines()[-1])
    assert report.parent == destination
    assert report.name.startswith("inbox-coverage-")
    assert "Inbox URL coverage report" in report.read_text()


@pytest.mark.parametrize(
    "protected",
    [
        "unselected_note",
        "response",
        "database",
        "database-wal",
        "database-shm",
        "database-journal",
    ],
)
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_output_protects_all_corpus_identities(tmp_path, protected, alias):
    inbox, db, note, _ = _run(tmp_path, note_text=URL)
    victim = (
        Path(str(db) + protected.removeprefix("database"))
        if protected.startswith("database")
        else tmp_path / "retained.data"
    )
    if protected != "database":
        victim.write_bytes(b"" if protected.startswith("database") else b"retained private bytes")
    if protected == "unselected_note":
        (inbox / "other.md").symlink_to(victim)
    elif protected == "response":
        _insert(db, inbox / "absent.md", response_path=str(victim))
    output = victim
    if alias != "direct":
        output = tmp_path / "alias.md"
        if alias == "symlink":
            output.symlink_to(victim)
        else:
            output.hardlink_to(victim)
    before = victim.read_bytes()
    assert (
        _rep.main(
            ["--inbox", str(inbox), "--db", str(db), "--file", note.name, "--out", str(output)]
        )
        == 2
    )
    assert victim.read_bytes() == before


def test_missing_referenced_response_is_still_protected(tmp_path):
    inbox, db, note, _ = _run(tmp_path, note_text=URL)
    response = tmp_path / "not-yet-created.data"
    _insert(db, note, response_path=str(response))
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(response)]) == 2
    assert not response.exists()


def test_protection_does_not_read_unselected_notes(tmp_path, monkeypatch):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    other = inbox / "other.md"
    other.write_text("unselected body")
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path != other, "protection must enumerate metadata, not bodies"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    output = tmp_path / "report.md"
    output.write_text("old report")
    assert (
        _rep.main(
            ["--inbox", str(inbox), "--db", str(db), "--file", "note.md", "--out", str(output)]
        )
        == 0
    )
    assert "Inbox URL coverage report" in output.read_text()


def test_identity_lookup_failure_cannot_allow_overwrite(tmp_path, monkeypatch):
    inbox, db, note, _ = _run(tmp_path, note_text=URL)
    response = tmp_path / "retained.data"
    response.write_text("retain")
    _insert(db, note, response_path=str(response))
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == response:
            raise PermissionError("synthetic inaccessible identity")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    output = tmp_path / "report.md"
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(output)]) == 2
    assert not output.exists()


def test_output_directory_inside_inbox_remains_refused(tmp_path):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    destination = inbox / "reports"
    destination.mkdir()
    assert _rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(destination)]) == 2
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("hidden", [False, True])
def test_unreadable_inventory_is_unknown_not_empty(tmp_path, monkeypatch, hidden):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    directory = inbox / (".hidden" if hidden else "restricted")
    directory.mkdir()
    victim = directory / "other.md"
    victim.write_text("retain entire note")
    output = tmp_path / "alias.md"
    output.hardlink_to(victim)
    if hidden:
        output = tmp_path / "ordinary-report.md"
    original = os.scandir

    def scan(path):
        if Path(path) == directory:
            raise PermissionError("synthetic inaccessible subtree")
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert _rep.main(
        [
            "--inbox",
            str(inbox),
            "--db",
            str(db),
            "--file",
            "note.md",
            "--out",
            str(output),
        ]
    ) == (0 if hidden else 2)
    assert victim.read_text() == "retain entire note"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory search permission")
def test_unstattable_note_cannot_allow_overwrite(tmp_path, monkeypatch):
    # Python 3.14 implements Path.is_file as os.path.isfile, which hides every
    # OSError; emulate it so the gap shows on every supported interpreter.
    monkeypatch.setattr(Path, "is_file", lambda path, **_: os.path.isfile(path))
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    directory = inbox / "restricted"
    directory.mkdir()
    victim = directory / "other.md"
    victim.write_text("retain entire note")
    output = tmp_path / "alias.md"
    output.hardlink_to(victim)
    directory.chmod(0o600)  # listable, not searchable: names visible, stat fails
    try:
        rc = _rep.main(
            ["--inbox", str(inbox), "--db", str(db), "--file", "note.md", "--out", str(output)]
        )
    finally:
        directory.chmod(0o700)
    assert rc == 2
    assert victim.read_text() == "retain entire note"


def test_unreadable_scanner_excluded_directory_does_not_block(tmp_path, monkeypatch):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    directory = inbox / "_genesis"
    directory.mkdir()
    original = os.scandir

    def scan(path):
        if Path(path) == directory:
            raise PermissionError("synthetic inaccessible excluded subtree")
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    output = tmp_path / "report.md"
    assert (
        _rep.main(
            ["--inbox", str(inbox), "--db", str(db), "--file", "note.md", "--out", str(output)]
        )
        == 0
    )
    assert "Inbox URL coverage report" in output.read_text()


def _report_rc(inbox: Path, db: Path, output: Path) -> int:
    return _rep.main(
        ["--inbox", str(inbox), "--db", str(db), "--file", "note.md", "--out", str(output)]
    )


def test_unreadable_tilde_directory_refuses(tmp_path, monkeypatch):
    # The scanner skips a tilde ENTRY but still descends into its folder.
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    directory = inbox / "~archive"
    directory.mkdir()
    original = os.scandir

    def scan(path):
        if Path(path) == directory:
            raise PermissionError("synthetic inaccessible eligible subtree")
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    output = tmp_path / "report.md"
    assert _report_rc(inbox, db, output) == 2
    assert not output.exists()


def test_note_under_tilde_directory_is_protected(tmp_path):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    victim = inbox / "~archive" / "kept.md"
    victim.parent.mkdir()
    victim.write_text("retain entire note")
    output = tmp_path / "alias.md"
    output.hardlink_to(victim)
    assert _report_rc(inbox, db, output) == 2
    assert victim.read_text() == "retain entire note"


def test_every_scanned_note_is_in_the_inventory(tmp_path):
    from genesis.inbox.scanner import scan_folder

    inbox = tmp_path / "inbox"
    for rel in (
        "note.md",
        "a/b.md",
        "~archive/kept.md",
        "a/~sub/deep.md",
        "_genesis/r.genesis.md",
        "_private/y.md",
        ".hidden/z.md",
        "c.sync-conflict-1.md",
        "notes.txt",
        "a/~sub/clip.txt",
    ):
        path = inbox / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(URL)
    scanned = set(scan_folder(inbox, recursive=True))
    assert {inbox / "~archive" / "kept.md", inbox / "notes.txt"} <= scanned
    assert scanned <= set(_rep._inbox_files(inbox))
    assert {path for path in scanned if path.suffix == ".md"} <= set(_rep._note_paths(inbox))
    assert inbox / "notes.txt" not in _rep._note_paths(inbox)


def test_self_referential_symlink_note_does_not_block(tmp_path):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    loop = inbox / "loop.md"
    loop.symlink_to(loop)
    output = tmp_path / "report.md"
    assert _report_rc(inbox, db, output) == 0
    assert "Inbox URL coverage report" in output.read_text()


def test_hardlink_to_inbox_text_file_is_refused(tmp_path):
    # The monitor evaluates every file type, not only markdown notes.
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    victim = inbox / "clip.txt"
    victim.write_text("retain entire file")
    output = tmp_path / "alias.md"
    output.hardlink_to(victim)
    assert _report_rc(inbox, db, output) == 2
    assert victim.read_text() == "retain entire file"


def test_inbox_text_symlink_target_is_refused(tmp_path):
    inbox, db, _, _ = _run(tmp_path, note_text=URL)
    victim = tmp_path / "outside.data"
    victim.write_text("retain entire file")
    (inbox / "linked.txt").symlink_to(victim)
    assert _report_rc(inbox, db, victim) == 2
    assert victim.read_text() == "retain entire file"
