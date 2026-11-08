"""Closed control witnesses bind the actual owner independently of native results."""

from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _data():
    identity = {"pid": 123, "start_ticks": 10,
                "boot_id": "00000000-0000-0000-0000-000000000001"}
    return {"state": "clean", "reason": "normal", "reservation_sha256": "a" * 64,
            "observer": identity, "observed_ns": 1000,
            "foreground": {"identity": {**identity, "pid": 124}, "returncode": 0},
            "monitor": {"identity": {**identity, "pid": 125}, "returncode": 0}, "stop": None}


def _validate(queue, data):
    operation = getattr(queue._recovery, "control_data", None)
    assert callable(operation), "control closure requires one complete closed validator"
    return operation(data, queue._native, queue._proof)


@pytest.mark.parametrize("field", tuple(_data()))
def test_every_required_control_field_is_enforced(queue, field):
    data = _data()
    del data[field]
    with pytest.raises(ValueError):
        _validate(queue, data)


@pytest.mark.parametrize("field,value", [
    ("extra", 0), ("state", "complete"), ("state", None), ("reason", "owner_died"),
    ("reservation_sha256", "A" * 64), ("reservation_sha256", "a" * 63),
    ("observed_ns", True), ("observed_ns", 0), ("observed_ns", 2**63),
    ("observer", None), ("observer", {"pid": 1}), ("stop", {}),
    ("foreground", {}), ("monitor", {"identity": _data()["observer"], "returncode": True}),
    ("foreground", {"identity": _data()["observer"], "returncode": 2**63}),
    ("foreground", {"identity": _data()["observer"], "returncode": -2**63 - 1}),
])
def test_invalid_control_domains_never_normalize_to_clean(queue, field, value):
    with pytest.raises(ValueError):
        _validate(queue, {**_data(), field: value})


@pytest.mark.parametrize("child", ["observer", "foreground", "monitor"])
@pytest.mark.parametrize("field,value", [("pid", True), ("pid", 0), ("start_ticks", 2**63),
                                        ("boot_id", "not-a-boot"), ("extra", 1)])
def test_every_retained_process_identity_is_closed(queue, child, field, value):
    data = deepcopy(_data())
    identity = data[child] if child == "observer" else data[child]["identity"]
    identity[field] = value
    with pytest.raises(ValueError):
        _validate(queue, data)


@pytest.mark.parametrize("state,reason", [("clean", "normal"), ("uncertain", "monitor_failed"),
                                         ("uncertain", "owned_wait_unavailable"), ("orphaned", "owner_died")])
@pytest.mark.parametrize("returncode", [-2**63, -9, 0, 5, 2**63 - 1, None])
def test_control_status_is_not_provider_success(queue, state, reason, returncode):
    data = _data()
    data.update(state=state, reason=reason)
    data["foreground"]["returncode"] = returncode
    if state == "clean" and returncode is None:
        with pytest.raises(ValueError):
            _validate(queue, data)
    else:
        assert _validate(queue, data) == data


def test_embedded_stop_uses_existing_closed_stop_contract(queue):
    data = _data()
    data["stop"] = {"reason": "pressure_stop", "observed_ns": 999,
                    "boot_id": data["observer"]["boot_id"], "unit": "reserved.scope"}
    assert _validate(queue, data) == data
    data["stop"]["reason"] = "guessed_oom"
    with pytest.raises(ValueError):
        _validate(queue, data)
