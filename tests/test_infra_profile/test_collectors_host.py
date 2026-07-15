"""Tests for the host-plane collector (infra_profile/collectors/host.py)."""

from __future__ import annotations

from genesis.infra_profile.collectors.host import HOST_SECTIONS, collect_host
from genesis.infra_profile.types import (
    PLANE_HOST,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)

# The real GuardianRemote.host_profile() returns the gateway JSON verbatim on
# success and {"ok": False, "action": ..., "error": ...} on failure — fakes
# below implement exactly that contract (real method name, real return shape).

_LIVE_BLOB = {
    "ok": True,
    "action": "host-profile",
    "host_system": {
        "mem_total_kb": 21508924,
        "mem_available_kb": 7394656,
        "swap_total_kb": 7712764,
        "swap_free_kb": 4508388,
        "nproc": 5,
        "kernel_release": "6.8.0-134-generic",
        "architecture": "x86_64",
        "hostname": "guardian-host",
        "os_pretty_name": "Ubuntu 24.04.4 LTS",
        "uptime_seconds": 850000.0,
        "loadavg": [8.49, 16.36, 11.05],
    },
    "host_storage_pool": {
        "detected": True,
        "data_pct": 61.19,
        "metadata_pct": 42.6,
        "vg_free_bytes": 34359738368,
        "pool_used_pct": None,
        "detail": "lvm vg0 data=61.19 meta=42.6",
        "tier": "ok",
        "pool_name": "default",
    },
    "host_virt": {
        "incus_client_version": "6.0.0",
        "incus_server_version": "6.0.0",
        "container_name": "genesis",
        "container_limits": {"limits.cpu": "8", "limits.memory": "16GiB"},
        "detect_virt": "kvm",
        "pve_version": None,
        "smartctl_present": False,
    },
}


class _FakeRemote:
    def __init__(self, response):
        self._response = response

    async def host_profile(self) -> dict:
        return self._response


class _RaisingRemote:
    async def host_profile(self) -> dict:
        raise OSError("ssh binary missing")


async def test_no_guardian_is_unavailable() -> None:
    available, reason, sections = await collect_host(None)
    assert available is False
    assert "no guardian" in reason
    assert [s.name for s in sections] == list(HOST_SECTIONS)
    assert all(s.status == STATUS_UNAVAILABLE and s.plane == PLANE_HOST for s in sections)


async def test_denied_gateway_is_unavailable_with_friendly_reason() -> None:
    # Realistic contract: _as_json embeds the gateway's RAW stderr JSON in the
    # error field on rc=1 — the quoted "denied" sentinel lives inside it.
    remote = _FakeRemote({
        "ok": False,
        "action": "host-profile",
        "error": '{"ok": false, "error": "denied"}',
    })
    available, reason, sections = await collect_host(remote)
    assert available is False
    assert "not deployed" in reason
    assert all(s.status == STATUS_UNAVAILABLE for s in sections)


async def test_ssh_auth_failure_not_masked_as_not_deployed() -> None:
    # 'Permission denied (publickey)' contains bare 'denied' but NOT the
    # quoted gateway sentinel — it must surface as itself, not as a benign
    # not-deployed message (review 2026-07-13).
    remote = _FakeRemote({
        "ok": False,
        "action": "host-profile",
        "error": "Permission denied (publickey,password)",
    })
    available, reason, sections = await collect_host(remote)
    assert available is False
    assert "Permission denied" in reason
    assert "not deployed" not in reason


async def test_ssh_failure_is_unavailable() -> None:
    remote = _FakeRemote({"ok": False, "action": "host-profile", "error": "timeout"})
    available, reason, sections = await collect_host(remote)
    assert available is False
    assert reason == "timeout"
    assert all(s.status == STATUS_UNAVAILABLE for s in sections)


async def test_raising_remote_degrades_not_raises() -> None:
    available, reason, sections = await collect_host(_RaisingRemote())
    assert available is False
    assert "ssh binary missing" in reason
    assert all(s.status == STATUS_UNAVAILABLE for s in sections)


async def test_live_blob_splits_facts_and_metrics() -> None:
    available, reason, sections = await collect_host(_FakeRemote(_LIVE_BLOB))
    assert available is True
    assert reason is None
    by_name = {s.name: s for s in sections}
    assert set(by_name) == set(HOST_SECTIONS)
    assert all(s.status == STATUS_OK and s.plane == PLANE_HOST for s in sections)

    system = by_name["host_system"]
    assert system.facts == {
        "mem_total_kb": 21508924,
        "swap_total_kb": 7712764,
        "nproc": 5,
        "kernel_release": "6.8.0-134-generic",
        "architecture": "x86_64",
        "hostname": "guardian-host",
        "os_pretty_name": "Ubuntu 24.04.4 LTS",
    }
    # Volatile readings must NOT be hashed — they live in metrics.
    assert "loadavg" in system.metrics
    assert "mem_available_kb" in system.metrics
    assert "uptime_seconds" in system.metrics
    # Host swap: total is topology (its disappearance = the wedge
    # precondition, drift-worthy); free is a reading.
    assert "swap_free_kb" in system.metrics
    assert "swap_free_kb" not in system.facts

    pool = by_name["host_storage_pool"]
    assert pool.facts == {"detected": True, "pool_name": "default"}
    # `detail` embeds live percentages — a fact would churn every refresh.
    assert "detail" in pool.metrics
    assert "data_pct" in pool.metrics
    assert "tier" in pool.metrics

    virt = by_name["host_virt"]
    assert virt.facts["container_limits"] == {"limits.cpu": "8", "limits.memory": "16GiB"}
    assert virt.facts["detect_virt"] == "kvm"
    assert virt.metrics == {}


async def test_unknown_host_field_lands_in_metrics_not_facts() -> None:
    """A NEW field added host-side (version skew) must not churn fact hashes."""
    blob = {**_LIVE_BLOB, "host_system": {**_LIVE_BLOB["host_system"], "new_probe": 42}}
    _, _, sections = await collect_host(_FakeRemote(blob))
    system = next(s for s in sections if s.name == "host_system")
    assert "new_probe" not in system.facts
    assert system.metrics["new_probe"] == 42


async def test_host_side_section_error_becomes_error_section() -> None:
    blob = {**_LIVE_BLOB, "host_storage_pool": {"error": "RuntimeError('lvs exploded')"}}
    available, _, sections = await collect_host(_FakeRemote(blob))
    assert available is True  # plane is up; one section failed
    pool = next(s for s in sections if s.name == "host_storage_pool")
    assert pool.status == STATUS_ERROR
    assert "lvs exploded" in pool.error
    ok_sections = [s for s in sections if s.name != "host_storage_pool"]
    assert all(s.status == STATUS_OK for s in ok_sections)


async def test_partial_section_with_error_key_is_failed() -> None:
    # "error" is a reserved key at this boundary — even alongside data keys it
    # marks the section failed (never silently filed under metrics).
    blob = {
        **_LIVE_BLOB,
        "host_virt": {**_LIVE_BLOB["host_virt"], "error": "incus flaked mid-gather"},
    }
    _, _, sections = await collect_host(_FakeRemote(blob))
    virt = next(s for s in sections if s.name == "host_virt")
    assert virt.status == STATUS_ERROR
    assert "incus flaked" in virt.error


async def test_missing_section_in_blob_is_empty_ok() -> None:
    blob = {k: v for k, v in _LIVE_BLOB.items() if k != "host_virt"}
    _, _, sections = await collect_host(_FakeRemote(blob))
    virt = next(s for s in sections if s.name == "host_virt")
    assert virt.status == STATUS_OK
    assert virt.facts == {} and virt.metrics == {}
