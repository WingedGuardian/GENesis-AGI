"""Fresh owned audit receipts, including the real serial/xdist producer path."""

import hashlib
import json

import pytest

from tests import _live_writes_report as codec
from tests.test_live_writes import _SUBRUN_CONFTEST
from tests.test_live_writes_reporting import _subrun


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("with_hits", [False, True])
def test_native_census_replaces_old_identity_and_accounts_for_workers(tmp_path, monkeypatch, workers, with_hits):
    run_id = "owned-current-attempt-and-source"
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", run_id)
    status = tmp_path / "report.tsv.status.json"
    status.write_text(json.dumps({"schema": 1, "run_id": "owned-earlier-attempt"}))
    source = '''
import pytest
from tests import _live_writes as audit

@pytest.mark.parametrize("item", [0, 1])
def test_owned_census(tmp_path, item):
    assert item in (0, 1)
'''
    if with_hits:
        source += '''
    audit._S.real_home = str(tmp_path)
    audit._S.allowed_roots = ()
    (tmp_path / "owned-writer").write_bytes(b"owned")
'''
    payload, text = _subrun(tmp_path, _SUBRUN_CONFTEST, source, workers)
    receipt = json.loads(status.read_text())
    assert receipt.get("run_id") == run_id, "report must replace an earlier run's proof"
    assert receipt["schema"] == 1
    assert receipt["format"] == "escaped-tsv-v1"
    assert receipt["installed"] is True and receipt["disarmed"] == ""
    assert receipt["exitstatus"] == 0 and receipt["faults"] == 0
    assert receipt["unreported"] == []
    assert receipt["row_count"] == len(codec.decode(payload)) == (2 if with_hits else 0), text
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    expected = [f"gw{index}" for index in range(workers)]
    assert receipt["expected_workers"] == receipt["merged_workers"] == expected
    assert sorted(receipt["workers"]) == expected
    for worker in receipt["workers"].values():
        assert worker == {"run_id": run_id, "installed": True, "disarmed": "", "faults": 0, "exitstatus": 0}
