"""Checkpoint the existing ledger without mutating it or enrolling a session."""

import json
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_scripts.test_pr_verification_closer import _doc, _row, _seed

_tool = private_module("validator_checkpoint_under_test", Path(__file__).resolve().parents[2] / "scripts/validator_checkpoint.py")


def seed_evidence(tmp_path, doc, *, closed=False):
    path = tmp_path / "genesis.db"
    _seed(path)
    raw = json.dumps(doc) if not isinstance(doc, str) else doc
    with sqlite3.connect(path) as db:
        db.execute("UPDATE pr_verifications SET evidence=?, verdict=?, status=? WHERE pr_number=7",
                   (raw, "pass-with-measured-gaps", "closed" if closed else "open"))
    return path


def call(path, *extra, command="checkpoint"):
    return _tool.main([command, "--repo", "owner/repo", "--db-path", str(path),
                       *( ["--pr", "7"] if command == "checkpoint" else []), *extra])


@pytest.mark.parametrize("closed", [False, True])
def test_unlinked_measurement_is_reported_without_rewriting_even_closed_rows(tmp_path, capsys, closed):
    doc = _doc(gaps=[dict(kind="not_yet_done", detail="reachable check unfinished", command="run check")])
    path = seed_evidence(tmp_path, doc, closed=closed)
    before = _row(path)
    assert call(path) == 4
    report = json.loads(capsys.readouterr().out)
    assert not report["complete"] and report["read_only"]
    assert report["rows"][0]["pending"] and report["rows"][0]["status"] == before["status"]
    assert _row(path) == before


def test_pause_succeeds_without_hiding_pending_routing(tmp_path, capsys):
    path = seed_evidence(tmp_path, _doc(findings=[dict(summary="defect", disposition="chat")]))
    assert call(path, "--pause") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["paused"] and not report["complete"]


def test_pause_never_waits_for_public_receipt_network_reads(tmp_path, capsys, monkeypatch):
    from genesis.session_awareness import pr_tracking
    doc = _doc(findings=[dict(summary="task", disposition="filed")], tracking=[
        dict(kind="public", target="findings/0", url="https://github.com/owner/repo/issues/12",
             quote="task", rationale="Task covers this concern")])
    path = seed_evidence(tmp_path, doc)
    monkeypatch.setattr(pr_tracking, "public_receipt", lambda *a: pytest.fail("pause reached network"))
    assert call(path, "--pause") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["offline"] and not report["complete"]


@pytest.mark.parametrize("raw", ["not json", "{}", "null", '{"repo":"owner/repo","repo":"other/repo"}'])
def test_legacy_malformed_records_are_classified_without_echoing_private_inputs(tmp_path, capsys, raw):
    path = seed_evidence(tmp_path, raw, closed=True)
    before = _row(path)
    assert call(path, command="audit") == 4
    report = json.loads(capsys.readouterr().out)
    assert "manual classification" in report["rows"][0]["pending"][0]
    assert _row(path) == before


def _legacy(limits):
    """Evidence as written before ``gaps`` replaced ``scope_limits``: no ``gaps`` key."""
    doc = _doc()
    del doc["gaps"]
    return doc | {"scope_limits": limits}


@pytest.mark.parametrize(("limits", "expected"), [
    (["synthetic private limit text"], "Legacy evidence: 1 entries"),
    ([], "Legacy evidence: written with the retired scope_limits field (empty)"),
])
@pytest.mark.parametrize(("closed", "next_step"), [
    (True, "closed rows are never amended"),
    (False, "re-record it with gaps"),
])
def test_legacy_scope_limits_evidence_is_read_but_never_complete(
    tmp_path, capsys, limits, expected, closed, next_step
):
    path = seed_evidence(tmp_path, _legacy(limits), closed=closed)
    before = _row(path)
    assert call(path, command="audit") == 4
    output = capsys.readouterr().out
    row = json.loads(output)["rows"][0]
    assert row["complete"] is False and row["legacy_scope_limits"] == len(limits)
    assert row["pending"][0].startswith(expected) and next_step in row["pending"][0]
    assert "manual classification" not in output and "synthetic private" not in output
    assert _row(path) == before


def test_a_complete_document_does_not_become_complete_by_being_legacy(tmp_path, capsys):
    # Control: the same document in the current schema audits complete, so the legacy
    # line alone is what keeps the legacy one open.
    (tmp_path / "modern").mkdir()
    assert call(seed_evidence(tmp_path / "modern", _doc(), closed=True), command="audit") == 0
    capsys.readouterr()
    path = seed_evidence(tmp_path, _legacy([]), closed=True)
    assert call(path, command="audit") == 4
    assert json.loads(capsys.readouterr().out)["rows"][0]["pending"] == [
        _tool._legacy_pending(0, "closed")]


@pytest.mark.parametrize("bad", [
    {"scope_limits": [{"kind": "method"}]},   # not the legacy shape: entries were strings
    {"scope_limits": "one string"},
    {"scope_limits": [""]},                   # the retired field refused blank entries
    {"scope_limits": ["   "]},
    {"scope_limits": [7]},
    {"scope_limits": [], "claims": []},      # the rest of the document is invalid too
    {"scope_limits": [], "pr": 8},           # belongs to another row
    {"scope_limits": [], "repo": "other/repo"},  # belongs to another repository
    {"scope_limits": [], "gaps": []},        # written after gaps existed: not legacy
])
def test_anything_but_a_valid_legacy_document_stays_manual(tmp_path, capsys, bad):
    doc = _legacy([]) | bad
    path = seed_evidence(tmp_path, doc, closed=True)
    assert call(path, command="audit") == 4
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert "manual classification" in row["pending"][0] and "legacy_scope_limits" not in row


def test_mismatched_row_identity_and_missing_selected_row_do_not_pass(tmp_path, capsys):
    path = seed_evidence(tmp_path, _doc(pr=8))
    assert call(path, "--pr", "99") == 4
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 2 and all(not row["complete"] for row in rows)


def test_explicit_accepted_limit_can_complete_a_legacy_gap_without_writes(tmp_path, capsys):
    doc = _doc(gaps=[dict(kind="method", detail="observation only")], tracking=[
        dict(kind="accepted_limit", target="gaps/0", rationale="Method limit accepted; no work remains")])
    path = seed_evidence(tmp_path, doc, closed=True)
    before = _row(path)
    assert call(path, command="audit") == 0
    assert json.loads(capsys.readouterr().out)["complete"]
    assert _row(path) == before


@pytest.mark.parametrize("offline", [False, True])
def test_live_and_offline_share_assessor_without_exposing_supporting_evidence(tmp_path, capsys, monkeypatch, offline):
    from genesis.session_awareness import pr_tracking
    private_evidence = "synthetic private identifier retained locally"
    doc = _doc(findings=[dict(summary="acceptance task", disposition="issue")], tracking=[
        dict(kind="public", target="findings/0", url="https://github.com/owner/repo/issues/12",
             quote="public acceptance task", rationale="Covers the required acceptance")])
    doc["claims"][0]["measurement"] = private_evidence
    path = seed_evidence(tmp_path, doc)
    calls = []
    def reader(*args):
        calls.append(args)
        return "public acceptance task"
    monkeypatch.setattr(pr_tracking, "public_receipt", reader)
    assert call(path, *( ["--offline"] if offline else [])) == (4 if offline else 0)
    output = capsys.readouterr().out
    assert private_evidence not in output
    assert bool(calls) is not offline
    assert json.loads(output)["complete"] is not offline


def test_unavailable_database_never_traps_a_pause(tmp_path, capsys):
    path = tmp_path / "missing.sqlite"
    assert call(path, "--pause") == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["complete"] and report["read_only"]
    assert not path.exists()


def test_cli_preflight_reports_missing_private_writer_without_disabling_public_route(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(_tool.shutil, "which", lambda name: "/synthetic/gh")
    monkeypatch.setattr(_tool, "_gh_json", lambda endpoint: {"private": False, "permissions": {"push": True}})
    assert call(tmp_path / "missing.sqlite", command="preflight") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["public_writer"]["available"] and not report["private_writer"]["available"]
    assert not report["database_exists"]
