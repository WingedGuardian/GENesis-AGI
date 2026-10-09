"""Receipt-bound identity/refusal controls and real verification CLI outcomes."""

import asyncio
import copy
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from xml.etree.ElementTree import tostring

import pytest

from tests.conftest import private_module
from tests.test_scripts import test_codex_validator_pilot as pilot_tests
from tests.test_scripts.test_pr_verification_closer import _doc_for, _seed
from tests.test_scripts.test_pr_verification_preview import _bytes

ROOT = Path(__file__).resolve().parents[2]
pilot = pilot_tests.pilot
configured = pilot_tests.configured


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return private_module("validator_preview_under_test", ROOT / "scripts/codex_validator_preview.py")


@pytest.fixture
def ready(preview, pilot, configured, monkeypatch):
    workspace, config = configured
    row = config["rows"][0]
    state = {"bracket": "b1-" + "1" * 24, "established": True}
    receipt = {
        "version": 1, "configuration": pilot.digest(config), "row": row,
        "cases": pilot.measured_cases(tostring(pilot_tests._xml(pilot)), row["recipe"]),
        "before": state, "after": {**state, "verified": True},
        "scope_limit": pilot.RECIPES[row["recipe"]][2],
    }
    target = workspace / ".codex/receipts/7.json"
    pilot._publish_receipt(target, receipt)
    monkeypatch.setattr(preview, "deployment_lock", nullcontext)
    monkeypatch.setattr(preview, "bound_state", lambda *a, **k: {**state, "verified": True})
    calls = []

    async def cli(*args):
        calls.append(args)
        return "DRY RUN"

    monkeypatch.setattr(preview, "_cli", cli)
    payload = {
        "version": 1, "operation": "pilot_preview", "pr": 7,
        "receipt": pilot.digest(receipt), "evidence": _doc_for("pass-mechanical", merge_commit=row["merge_commit"]),
        "note": None, "park": False,
    }
    return workspace, payload, receipt, target, calls


def test_completed_receipt_only_previews_and_forces_fixture_gap(preview, ready):
    workspace, payload, receipt, _, calls = ready
    result = preview.preview(workspace, ROOT, payload)
    assert result == {"pr": 7, "receipt": payload["receipt"], "preview_only": True, "preview": "DRY RUN"}
    assert calls[0][3].scope_limits == [receipt["scope_limit"]]
    assert payload["evidence"]["scope_limits"] == []


@pytest.mark.parametrize("field,value", [
    ("pr", True), ("pr", 0), ("receipt", "a" * 63), ("receipt", "A" * 64),
    ("park", 0), ("park", "false"), ("note", []), ("note", ""), ("note", "x" * 8001),
])
def test_preview_refuses_invalid_closed_values(preview, ready, field, value):
    workspace, payload, _, _, calls = ready
    payload[field] = value
    with pytest.raises(ValueError):
        preview.preview(workspace, ROOT, payload)
    assert calls == []


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("pr", 8), ("merge_commit", "c" * 40)])
def test_preview_refuses_different_evidence_identity(preview, ready, field, value):
    workspace, payload, _, _, calls = ready
    payload["evidence"][field] = value
    with pytest.raises(ValueError):
        preview.preview(workspace, ROOT, payload)
    assert calls == []


@pytest.mark.parametrize("fault", ["missing", "digest", "config", "row", "cases", "duplicate", "scope", "extra", "bracket"])
def test_preview_refuses_absent_stale_or_incomplete_receipt(preview, pilot, ready, fault):
    workspace, payload, receipt, target, calls = ready
    changed = copy.deepcopy(receipt)
    if fault == "missing":
        target.unlink()
    elif fault == "digest":
        payload["receipt"] = "0" * 64
    else:
        if fault == "config":
            changed["configuration"] = "0" * 64
        elif fault == "row":
            changed["row"]["intent"] = "changed"
        elif fault == "cases":
            changed["cases"].pop()
        elif fault == "duplicate":
            changed["cases"].append(changed["cases"][0])
        elif fault == "scope":
            changed["scope_limit"] = "none"
        elif fault == "extra":
            changed["write"] = True
        elif fault == "bracket":
            changed["before"]["established"] = False
        pilot._publish_receipt(target, changed)
        payload["receipt"] = pilot.digest(changed)
    with pytest.raises((ValueError, OSError)):
        preview.preview(workspace, ROOT, payload)
    assert calls == []


@pytest.mark.parametrize("verdict,park", [("pass-mechanical", False), ("fail-intent", False), ("pass-mechanical", True)])
def test_actual_preview_cli_preserves_wal_and_open_row(preview, tmp_path, monkeypatch, verdict, park):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # Directory link preserves the real virtualenv interpreter prefix.
    (runtime / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    (runtime / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    (runtime / "src").symlink_to(ROOT / "src", target_is_directory=True)
    workspace = tmp_path / "workspace"
    (workspace / ".codex").mkdir(parents=True, mode=0o700)
    ledger = tmp_path / "ledger.db"
    _seed(ledger)
    producer = subprocess.run([
        sys.executable, "-c", "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
        "c.execute(\"UPDATE pr_verifications SET pr_title='WAL fixture'\"); c.commit(); os._exit(0)",
        str(ledger),
    ], capture_output=True, timeout=20)
    assert producer.returncode == 0, producer.stderr
    before = _bytes(ledger)
    monkeypatch.setattr(preview, "child_environment", lambda: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GENESIS_HOME": str(tmp_path / "private")
    })
    doc = preview.parse_evidence(_doc_for(verdict, scope_limits=["fixtures only"]))
    note = "fixture judgment" if park or verdict == "fail-intent" else None
    output = asyncio.run(preview._cli(runtime, workspace, {"ledger": str(ledger)}, doc, note, park))
    assert "DRY RUN" in output
    assert ("cannot-verify" if park else "fail-intent" if verdict == "fail-intent" else "pass-with-measured-gaps") in output
    assert _bytes(ledger) == before
    assert list((workspace / ".codex").iterdir()) == []


def test_closed_dispatch_refuses_preview_extra_key_before_entry(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    request = private_module("preview_request_under_test", ROOT / "scripts/codex_validator_request.py")
    with pytest.raises(ValueError):
        request.execute({"version": 1, "operation": "pilot_preview", "pr": 7, "dry_run": False})
