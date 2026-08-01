"""ResilienceState network axis — the PR-2 additions to the state machine."""

from __future__ import annotations

from genesis.resilience.state import (
    NetworkStatus,
    ResilienceStateMachine,
    TmpPressureStatus,
)
from genesis.routing.types import DegradationLevel


def test_network_defaults_normal():
    sm = ResilienceStateMachine()
    assert sm.current.network == NetworkStatus.NORMAL


def test_update_network_applies():
    sm = ResilienceStateMachine()
    txns = sm.update_network(NetworkStatus.OFFLINE)
    assert len(txns) == 1
    assert txns[0].axis == "network"
    assert sm.current.network == NetworkStatus.OFFLINE


def test_is_any_degraded_counts_offline_via_base_expression():
    # F5: the sole consumer passes include_tmp_pressure=False — network-OFFLINE
    # must count there, so it lives in the BASE expression, not the tmp branch.
    sm = ResilienceStateMachine()
    sm.update_network(NetworkStatus.OFFLINE)
    assert sm.is_any_degraded(include_tmp_pressure=False) is True
    assert sm.is_any_degraded(include_tmp_pressure=True) is True


def test_is_any_degraded_ignores_degraded():
    # DEGRADED (slow but working) must NOT pause queue draining.
    sm = ResilienceStateMachine()
    sm.update_network(NetworkStatus.DEGRADED)
    assert sm.is_any_degraded(include_tmp_pressure=False) is False


def test_network_excluded_from_legacy_level():
    # PR-2: network must NOT feed the legacy degradation level (shedding is PR-3).
    sm = ResilienceStateMachine()
    sm.update_network(NetworkStatus.OFFLINE)
    assert sm.current.to_legacy_degradation_level() == DegradationLevel.NORMAL


def test_legacy_level_still_reflects_other_axes():
    # sanity: excluding network didn't break the existing mapping
    sm = ResilienceStateMachine()
    sm.update_tmp_pressure(TmpPressureStatus.CRITICAL)
    assert sm.current.to_legacy_degradation_level() == DegradationLevel.ESSENTIAL


def test_network_axis_opts_out_of_flap_protection():
    # Rapid flapping must NOT latch a held state (the sentinel owns hysteresis).
    sm = ResilienceStateMachine()
    for _ in range(6):
        sm.update_network(NetworkStatus.OFFLINE)
        sm.update_network(NetworkStatus.NORMAL)
    # ends NORMAL — never stuck holding OFFLINE despite >3 flips in the window
    assert sm.current.network == NetworkStatus.NORMAL
    assert sm.is_any_degraded(include_tmp_pressure=False) is False
