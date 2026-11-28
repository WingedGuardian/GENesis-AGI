"""Exercise close receipts and live tracking on owned CLI ledgers."""

import json

import pytest

from genesis.session_awareness.pr_recording import PrMetadata
from tests.fixtures.verification_sources import (
    CLOSE_CASES,
    MERGE_SHA,
    reviewed_close,
    simple_receipt,
    tracking_closes_during_acquisition,
)
from tests.test_scripts.test_pr_verification_closer import (
    _close_before_the_fixture_dates as _close_before_the_fixture_dates,
)
from tests.test_scripts.test_pr_verification_closer import (
    _doc,
    _prv,
    _row,
    _run,
    _seed,
    _with_tracking,
)
from tests.test_scripts.test_pr_verification_closer import (
    _no_real_github as _no_real_github,
)


@pytest.mark.parametrize("case", CLOSE_CASES)
@pytest.mark.parametrize("preview", [False, True])
def test_close_requires_an_independent_receipt(tmp_path, monkeypatch, capsys, case, preview):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    doc, source = reviewed_close(source_dir, monkeypatch, case)
    monkeypatch.setattr(_prv, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, source.body))
    db = tmp_path / "genesis.db"
    _seed(db)
    before = _row(db)
    flags = ["--dry-run"] if preview else []
    if reason := CLOSE_CASES[case]:
        assert _run(tmp_path, doc, *flags, route=False) == 2
        output = capsys.readouterr()
        assert reason in output.err and "Nothing was written" in output.err
        if preview and case == "missing":
            assert "Derived verdict: pass-mechanical (close refused)" in output.out
            assert "CLOSES the row" not in output.out
        assert _row(db) == before
        return
    assert _run(tmp_path, doc, *flags, route=False) == 0
    stored = _row(db)
    if preview:
        assert "CLOSES the row" in capsys.readouterr().out
        assert stored == before
        return
    assert stored["status"] == "closed"
    receipt = json.loads(stored["evidence"])["completeness"]
    assert receipt["complete"] is True
    assert receipt["evidence_sha256"] == doc["completeness"]["evidence_sha256"]
    assert receipt["review_execution_ref"] == "fixture-only:no-model-ran"
    assert _run(tmp_path, doc, route=False) == 1
    assert _row(db) == stored


@pytest.mark.parametrize("preview", [False, True])
def test_tracking_closed_during_source_acquisition(tmp_path, monkeypatch, capsys, preview):
    db = tmp_path / "genesis.db"
    _seed(db)
    before = _row(db)
    doc = simple_receipt(_with_tracking(_doc(
        findings=[{"summary": "reachable timer defect", "disposition": "filed"}],
    )))
    reads = tracking_closes_during_acquisition(monkeypatch)
    flags = ["--dry-run"] if preview else []
    assert _run(tmp_path, doc, *flags, route=False) == 4
    assert reads == ["tracking", "sources", "tracking"]
    assert "public task closed while acquiring PR sources" in capsys.readouterr().out
    stored = _row(db)
    if preview:
        assert stored == before
        return
    assert stored["status"] == "open"
    assert stored["verdict"] == "pass-mechanical"
    assert stored["attempt_count"] == before["attempt_count"] + 1
    assert json.loads(stored["evidence"])["findings"] == doc["findings"]
