"""Ordered proof retirement, failed publication and real failed-test receipts."""

import json
from pathlib import Path

import pytest

from tests import _live_writes as audit
from tests import _live_writes_report as codec
from tests.test_live_writes import _SUBRUN_CONFTEST
from tests.test_live_writes_reporting import _subrun


def _metadata(run_id):
    return {"run_id": run_id, "installed": True, "disarmed": "", "faults": 0, "exitstatus": 0,
            "unreported": [], "expected_workers": [], "merged_workers": [], "workers": {}}


def _assess(path, run_id, outcome="success", *, workers=0):
    from scripts.ci.live_writes_census import assess

    return assess(path, run_id=run_id, workers=workers, tests_outcome=outcome)


@pytest.mark.parametrize("stage", ["invalidate", "data", "status"])
def test_failed_publication_cannot_leave_an_earlier_proof_for_a_new_run(tmp_path, monkeypatch, stage):
    path = tmp_path / "rows.tsv"
    status = Path(str(path) + ".status.json")
    codec.publish(path, [], _metadata("earlier"))
    assert _assess(path, "earlier")["verdict"] == "clean"
    original_unlink, original_data, original_status = Path.unlink, Path.write_bytes, Path.write_text

    def unlink(target, *args, **kwargs):
        if target == status and stage == "invalidate":
            raise PermissionError("owned publication fault")
        return original_unlink(target, *args, **kwargs)

    def write_data(target, *args, **kwargs):
        if target == path and stage == "data":
            raise PermissionError("owned publication fault")
        return original_data(target, *args, **kwargs)

    def write_status(target, *args, **kwargs):
        if target == status and stage == "status":
            raise PermissionError("owned publication fault")
        return original_status(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "write_bytes", write_data)
    monkeypatch.setattr(Path, "write_text", write_status)
    row = ("test::owned", "~/.genesis", "write", "/owned")
    with pytest.raises(PermissionError, match="owned publication fault"):
        codec.publish(path, [row], _metadata("current"))
    assert _assess(path, "current")["verdict"] == "incomplete"
    if stage != "invalidate":
        assert not status.exists()


def test_unidentified_rewrite_cannot_qualify_an_earlier_proof_for_new_data(tmp_path):
    path = tmp_path / "rows.tsv"
    codec.publish(path, [], _metadata("earlier"))
    status = Path(str(path) + ".status.json")
    old_proof = status.read_bytes()
    row = ("test::owned", "~/.genesis", "write", "/owned")
    codec.publish(path, [row], _metadata(None))
    assert codec.decode(path.read_bytes()) == [row]
    assert status.read_bytes() == old_proof
    assert _assess(path, "current")["verdict"] == "incomplete"
    assert _assess(path, "earlier")["verdict"] == "incomplete"


def test_unidentified_transport_preserves_an_unrelated_companion_file(tmp_path):
    path = tmp_path / "rows.tsv"
    companion = Path(str(path) + ".status.json")
    companion.write_bytes(b"owned unrelated companion")
    codec.publish(path, [], _metadata(None))
    assert path.read_bytes() == b""
    assert companion.read_bytes() == b"owned unrelated companion"


def test_install_captures_identity_even_when_observation_cannot_arm(monkeypatch):
    from tests import _live_fence

    hooks = []
    monkeypatch.setattr(audit._S, "installed", False)
    monkeypatch.setattr(audit._S, "disarmed", "")
    monkeypatch.setattr(audit._S, "census_id", None)
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", "owned-unarmed")
    monkeypatch.setattr(_live_fence._S, "installed", True)
    monkeypatch.setattr(_live_fence._S, "real_home", None)
    monkeypatch.setattr(audit.sys, "addaudithook", hooks.append)
    audit.install(type("C", (), {"option": type("O", (), {"basetemp": None})()})())
    assert audit._S.census_id == "owned-unarmed"
    assert not audit._S.installed and audit._S.disarmed and hooks == []


def test_real_failed_pytest_receipt_cannot_qualify_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", "owned-failed-run")
    source = '''
def test_owned_failure():
    assert False, "owned expected test failure"
'''
    with pytest.raises(AssertionError, match="owned expected test failure"):
        _subrun(tmp_path, _SUBRUN_CONFTEST, source, 0)
    path = tmp_path / "report.tsv"
    receipt = json.loads(Path(str(path) + ".status.json").read_text())
    assert receipt["run_id"] == "owned-failed-run" and receipt["exitstatus"] == 1
    assert _assess(path, "owned-failed-run")["verdict"] == "incomplete"
    assert _assess(path, "owned-failed-run", "failure")["verdict"] == "incomplete"


def test_ordinary_worker_status_change_cannot_be_certified_as_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", "owned-worker-status")
    (tmp_path / "owned_status.py").write_text('''
def pytest_sessionfinish(session):
    if hasattr(session.config, "workeroutput"):
        session.exitstatus = 1
''')
    conftest = _SUBRUN_CONFTEST.replace('["tests._live_writes"]', '["owned_status", "tests._live_writes"]')
    _subrun(tmp_path, conftest, "def test_owned_pass():\n    assert 2 + 2 == 4\n", 1)
    path = tmp_path / "report.tsv"
    receipt = json.loads(Path(str(path) + ".status.json").read_text())
    assert receipt["exitstatus"] == 0  # xdist's earlier transport value stays zero
    verdict = _assess(path, "owned-worker-status", workers=1)
    assert receipt["workers"]["gw0"]["exitstatus"] == 1, verdict
    assert verdict["verdict"] == "incomplete"


def test_snapshot_reads_current_session_status_instead_of_the_earlier_argument(monkeypatch):
    monkeypatch.setattr(audit._S, "census_id", "owned-status-component")
    monkeypatch.setattr(audit._S, "installed", True)
    monkeypatch.setattr(audit._S, "disarmed", "")
    monkeypatch.setattr(audit._S, "faults", 0)
    monkeypatch.setattr(audit._S, "hits", {})
    monkeypatch.setattr(audit._S, "exitstatus", None)
    config = type("C", (), {"workeroutput": {}})()
    session = type("S", (), {"config": config, "exitstatus": pytest.ExitCode.TESTS_FAILED})()
    audit.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert config.workeroutput["genesis_live_writes"]["census"]["exitstatus"] == 1
