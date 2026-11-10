"""Retained input association is separate from baseline and response evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from genesis.db.crud.inbox_items import BATCH_ITEMS_V2_PREFIX, serialize_batch_items
from tests.conftest import private_module

rep = private_module(
    "item_report", Path(__file__).resolve().parents[2] / "scripts/inbox_coverage_report.py"
)
URL = "https://example.com/a"


def run(tmp_path, text, rows=(), response=None):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    note = inbox / "note.md"
    note.write_text(text)
    db = tmp_path / "fixture.db"
    response_path = tmp_path / "response.genesis.md"
    if response is not None:
        response_path.write_text(response)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE inbox_items (file_path TEXT, status TEXT, "
            "error_message TEXT, response_path TEXT, processed_at TEXT, "
            "batch_items TEXT, evaluated_content TEXT)"
        )
        for batch, baseline, status in rows:
            conn.execute(
                "INSERT INTO inbox_items VALUES (?,?,?,?,?,?,?)",
                (
                    str(note),
                    status,
                    None,
                    str(response_path) if response is not None else None,
                    "2026-01-01",
                    batch,
                    baseline,
                ),
            )
    out = tmp_path / "report.md"
    before = db.read_bytes(), note.read_bytes()
    assert rep.main(["--inbox", str(inbox), "--db", str(db), "--out", str(out)]) == 0
    assert (db.read_bytes(), note.read_bytes()) == before
    return out.read_text()


def test_legacy_input_membership_is_reported_without_baseline_promotion(tmp_path):
    other = "https://example.com/b"
    md = run(tmp_path, URL + "\n" + other, [(URL, URL + "\n" + other, "completed")], response=URL)
    assert f"| {URL} | evaluated | present | source_not_recorded |" in md
    assert f"| {other} | unattributable |" in md
    assert "baseline_only" in md
    assert "legacy" in md
    assert "mentioned: yes" in md


@pytest.mark.parametrize(
    "payload",
    [
        '["https://example.com/a", 1]',
        "[]",
        '[" "]',
        "{}",
        "broken",
    ],
    ids=["mixed", "empty", "blank", "object", "syntax"],
)
def test_corrupt_versioned_input_never_falls_back_to_partial_membership(tmp_path, payload):
    md = run(tmp_path, URL, [(BATCH_ITEMS_V2_PREFIX + payload, URL, "completed")])
    assert f"| {URL} | unattributable |" in md
    assert "corrupt_v2" in md
    assert "0 evaluated" in md


def test_text_blocks_duplicate_occurrences_and_empty_notes_are_accounted(tmp_path):
    text = "A whole\nmultiline block\n\n" + URL + "\n\n" + URL
    md = run(
        tmp_path,
        text,
        [(serialize_batch_items(["A whole\nmultiline block", URL]), text, "completed")],
    )
    assert "Logical items: 3" in md
    assert "A whole\nmultiline block" in md
    assert md.count("stored match: yes") == 3


def test_changed_annotation_is_not_an_exact_item_match(tmp_path):
    md = run(
        tmp_path,
        "new intent\n" + URL,
        [(serialize_batch_items(["old intent\n" + URL]), URL, "completed")],
    )
    assert "stored match: no" in md
    assert "url_only" in md
    assert "item status: unattributable" in md


def test_text_only_note_and_directive_are_visible(tmp_path):
    md = run(tmp_path, "A whole\ntext block\n\n[Keep comparisons grounded]")
    assert "Logical items: 2" in md
    assert "A whole\ntext block" in md
    assert "kind: directive" in md
    assert "Notes with no URLs: 1" in md


def test_empty_note_counts_without_invented_items(tmp_path):
    md = run(tmp_path, "\n\n")
    assert "Logical items: 0" in md
    assert "All selected notes: 1" in md


@pytest.mark.parametrize("status", ["superseded", "other", None])
def test_historical_status_and_response_reference_are_preserved(tmp_path, status):
    md = run(
        tmp_path,
        URL,
        [(serialize_batch_items([URL]), URL, status)],
        response=f"**Source:** {URL}",
    )
    assert f"status: {status or 'unknown'}" in md
    assert f"| {URL} | unattributable |" in md
    assert "response.genesis.md" in md
    assert "0 evaluated" in md


def test_nontext_input_is_unreadable_not_missing(tmp_path):
    payload = serialize_batch_items([URL]).encode()
    md = run(tmp_path, URL, [(payload, URL, "completed")])
    assert "input: unreadable_input" in md
    assert f"| {URL} | unattributable |" in md


def test_versioned_baseline_only_membership_remains_unknown(tmp_path):
    other = "https://example.com/b"
    md = run(
        tmp_path,
        URL + "\n" + other,
        [(serialize_batch_items([URL]), URL + "\n" + other, "completed")],
    )
    assert f"| {other} | unattributable |" in md
    assert "baseline_only" in md


def test_baseline_only_item_cannot_be_called_never(tmp_path):
    other = "https://example.com/b"
    report = rep.build_report(
        [rep.NoteInput("note", "note", [URL], URL)],
        {"note": [rep.RowInput("completed", None, None, None, 1, [other],
                               item_texts=[other], baseline_urls=[URL])]},
        lambda _: None,
    )
    assert report.notes[0]["items"][0]["class"] == "unattributable"


@pytest.mark.parametrize("suffix", ["", "\n" + URL], ids=["text", "annotation"])
def test_changed_interior_indentation_is_not_exact_input(tmp_path, suffix):
    md = run(tmp_path, "one\n  two" + suffix,
             [(serialize_batch_items(["one\ntwo" + suffix]), "", "completed")])
    assert "stored match: no" in md
    assert f"item status: {'unattributable' if suffix else 'never'}" in md


def test_text_baseline_only_membership_is_uncertain(tmp_path):
    text = "A whole\ntext block"
    md = run(tmp_path, text, [(serialize_batch_items([URL]), text, "completed")])
    assert "stored match: no" in md
    assert "item status: unattributable" in md


@pytest.mark.parametrize("text,baseline", [
    ("zeta\nalpha", "alpha\nzeta"),
    ("alpha\n\nbeta", "alpha\nbeta"),
    ("one\n  two", "one\ntwo"),
    ("[Keep comparisons grounded]", "[Keep comparisons grounded]\nother"),
], ids=["sorted", "boundaries-lost", "indentation-lost", "directive"])
def test_lossy_baseline_lines_preserve_text_history_uncertainty(tmp_path, text, baseline):
    md = run(tmp_path, text, [(serialize_batch_items([URL]), baseline, "completed")])
    assert "item status: never" not in md
    assert "item status: evaluated" not in md
    assert "item status: unattributable" in md


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "superseded", "other", None])
@pytest.mark.parametrize("payload", [None, "", b"opaque", BATCH_ITEMS_V2_PREFIX + "broken"],
                         ids=["missing", "blank", "nontext", "corrupt"])
def test_unknown_input_history_is_not_proof_of_never(tmp_path, status, payload):
    md = run(tmp_path, URL, [(payload, "", status)])
    assert f"| {URL} | unattributable |" in md
    assert "item status: unattributable" in md


def test_different_response_rows_do_not_pool_source_coverage():
    other = "https://example.com/b"
    item = URL + " " + other
    decoded = rep._row_item_urls(serialize_batch_items([item]))
    rows = [
        rep.RowInput("completed", None, path, "2026-01-01", n, *decoded)
        for n, path in enumerate(("old", "new"), 1)
    ]
    response = {"old": f"**Source:** {other}", "new": f"**Source:** {URL}"}
    report = rep.build_report(
        [rep.NoteInput("note", "note", [URL, other], item)], {"note": rows}, response.get
    )
    entries = report.notes[0]["urls"]
    assert entries[0]["covered"] == "yes"
    assert entries[1]["covered"] == "no"
    assert {entry["rowid"] for entry in entries} == {2}


@pytest.mark.parametrize("source,mentioned", [("unrelated", "no"), (URL, "yes")])
def test_source_contract_absence_is_distinct_from_incidental_mention(tmp_path, source, mentioned):
    md = run(tmp_path, URL, [(serialize_batch_items([URL]), URL, "completed")], response=source)
    assert "source_not_recorded" in md
    assert f"mentioned: {mentioned}" in md
