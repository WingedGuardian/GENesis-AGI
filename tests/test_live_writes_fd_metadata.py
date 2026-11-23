"""Unusable premetadata must not hide a successful native protected write."""

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import home as home
from tests.test_live_writes_fd import _write


@pytest.mark.parametrize("failure", [PermissionError, ValueError, None])
@pytest.mark.parametrize("protected", [False, True])
def test_failed_parent_probe_keeps_native_outcome_and_counts_uncertainty(home, monkeypatch, failure, protected):
    allowed = home.parent / "allowed"
    destination = home / ".genesis" if protected else allowed
    monkeypatch.chdir(allowed if protected else home / "genesis")

    def unavailable(*args):
        if failure is not None:
            raise failure("PRIVATE_PARENT_METADATA")
        return None

    monkeypatch.setattr(audit._W, "_parent", unavailable)
    audit._S.hits.clear()
    audit._S.faults = 0
    _write(destination)
    assert audit._S.faults >= 1
    assert any(row[1] == "observer fault" for row in audit._S.hits)
    assert "PRIVATE_PARENT_METADATA" not in repr(audit._S.hits)
    assert (destination / "owned").read_bytes() == b"owned synthetic bytes"
