"""Grant limits apply to issuance and retained authority without truncation."""

from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_retry_grant import NOW, candidate, helper

MAX_LIFETIME = 14_400 * 10**9


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("duration,valid", [(1, True), (MAX_LIFETIME, True),
                                            (MAX_LIFETIME + 1, False), (2 * MAX_LIFETIME, False)])
def test_grant_lifetime_is_bounded_at_issue(queue, tmp_path, provider, duration, valid):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    data["not_after_wall_ns"] = NOW["wall_ns"] + duration
    with queue._decision_gate(native=True) as (db, finalize):
        if valid:
            grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
            assert grant["not_after_monotonic_ns"] - NOW["monotonic_ns"] == duration
        else:
            with pytest.raises(ValueError):
                retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
            assert db.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


def grant_value(queue, tmp_path, provider):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native,
                            queue._legs, now=NOW, finalize=finalize)
    return retry, grant


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_retained_authority_cannot_bypass_lifetime_validation(queue, tmp_path, provider):
    retry, grant = grant_value(queue, tmp_path, provider)
    value = deepcopy(grant)
    value["not_after_wall_ns"] = value["issued_wall_ns"] + MAX_LIFETIME + 1
    value["not_after_monotonic_ns"] = value["issued_monotonic_ns"] + MAX_LIFETIME + 1
    with pytest.raises(ValueError):
        retry.validate(value, queue._native)


REFERENCES = [
    pytest.param("a" * 4096, True, id="ascii-limit"),
    pytest.param("a" * 4097, False, id="ascii-too-long"),
    pytest.param("雪" * 1365 + "a", True, id="utf8-limit"),
    pytest.param("雪" * 1365 + "aa", False, id="utf8-too-long"),
    pytest.param(chr(0xD800), False, id="surrogate"),
    pytest.param("", False, id="empty"),
    pytest.param("a\0b", False, id="nul"),
    pytest.param(True, False, id="boolean"),
]


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("reference,valid", REFERENCES)
def test_reference_is_bounded_in_utf8_bytes_without_silent_cut(
    queue, tmp_path, provider, reference, valid
):
    retry = helper()
    data = {**candidate(queue, tmp_path, provider), "evidence_reference": reference}
    with queue._decision_gate(native=True) as (db, finalize):
        if valid:
            grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
            assert grant["evidence_reference"] == reference
        else:
            with pytest.raises(ValueError):
                retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
            assert db.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_retained_authority_cannot_bypass_reference_bound(queue, tmp_path, provider):
    retry, grant = grant_value(queue, tmp_path, provider)
    value = {**grant, "evidence_reference": "a" * 4097}
    with pytest.raises(ValueError):
        retry.validate(value, queue._native)
