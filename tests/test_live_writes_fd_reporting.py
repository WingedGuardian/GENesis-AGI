"""Real serial/xdist reports must reflect native directory-FD destinations."""

import json
from pathlib import Path

import pytest

from scripts.ci.live_writes_census import assess
from tests._live_writes_report import decode
from tests.test_live_writes import _SUBRUN_CONFTEST
from tests.test_live_writes_reporting import _subrun


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("protected", [False, True])
def test_native_descriptor_destinations_reach_the_strict_reader(tmp_path, monkeypatch, workers, protected):
    run_id = "owned-fd-witness"
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", run_id)
    repo = Path(__file__).resolve().parent.parent
    source = '''
import os
from pathlib import Path
import pytest
from tests import _live_writes as audit

@pytest.mark.parametrize("case", [0, 1])
def test_native_destination(tmp_path, monkeypatch, case):
    assert Path(audit.__file__).resolve() == Path(EXPECTED_SOURCE)
    assert audit._W.installed and os.open is audit._W.wrapped
    home = tmp_path / "home"
    (home / "genesis").mkdir(parents=True)
    (home / ".genesis").mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(audit._S, "real_home", str(home))
    monkeypatch.setattr(audit._S, "allowed_roots", (str(allowed),))
    monkeypatch.setattr(audit._S, "classes", ((str(home / ".genesis"), "~/.genesis"),
                                           (str(home / "genesis"), "deployed")))
    monkeypatch.setattr(audit._S, "credential_files", frozenset())
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset())
    destination = home / ".genesis" if PROTECTED else allowed
    monkeypatch.chdir(allowed if PROTECTED else home / "genesis")
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("owned", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            os.write(fd, b"owned native contents")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    assert (destination / "owned").read_bytes() == b"owned native contents"
'''.replace("EXPECTED_SOURCE", repr(str(repo / "tests/_live_writes.py"))).replace("PROTECTED", repr(protected))
    payload, _ = _subrun(tmp_path, _SUBRUN_CONFTEST, source, workers)
    rows = decode(payload)
    result = assess(tmp_path / "report.tsv", run_id=run_id, workers=workers, tests_outcome="success")
    assert result["verdict"] == ("nonempty" if protected else "clean")
    if protected:
        assert len(rows) == 2 and all(row[1:3] == ("~/.genesis", "write") for row in rows)
    else:
        assert rows == []
    state = json.loads((tmp_path / "report.tsv.status.json").read_text())
    assert state["installed"] and not state["disarmed"] and state["faults"] == 0
    assert len(state["merged_workers"]) == workers
