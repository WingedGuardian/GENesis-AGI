"""Silent-skip closure Phase 3 — infra protection posture alarm.

#1082 provisions systemd-oomd and #1083 reconciles the container swap knob,
but a STABLE unprotected box still produced no signal anywhere (infra_profile
only fires infrastructure_drift on a fact-hash CHANGE — observed live: a
sibling install ran for weeks with swap disabled and no systemd-oomd until a
memory spike wedged it). The posture check un-blinds that: missing memory-plane
protections raise one non-paging 'high' infrastructure_alert that supersedes on
posture change and auto-resolves on recovery; a stale profile (>3d) raises a
distinct posture-UNKNOWN alert instead of asserting from dead facts.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness import loop as _loop
from genesis.awareness.loop import (
    _check_infra_protection_posture,
    _infra_missing_protections,
    _resolve_infra_protection_posture,
)
from genesis.db.schema._tables import TABLES

SOURCE = "infra_protection_posture_monitor"


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["observations"])
    await db.commit()
    return db


async def _alerts(db) -> list[dict]:
    async with db.execute(
        "SELECT id, priority, resolved, content, content_hash, resolution_notes "
        "FROM observations WHERE source = ? AND type = 'infrastructure_alert'",
        (SOURCE,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _open(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if a["resolved"] == 0]


def _profile(
    *,
    swap_max: object = "max",
    oomd: object = True,
    host_swap_kb: object = 8_000_000,
    knob: object = "true",
    networkd_route: object = True,
    keepconfig: object = True,
    watchdog: object = True,
    cc_tmp_isolated: object = True,
    container: object = "lxc",
    age_days: float = 0.0,
    collected_at: object = "auto",
) -> dict:
    """Fabricate a profile in the real on-disk shape (sections.<plane>.facts)."""
    if collected_at == "auto":
        collected_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    profile: dict = {
        "sections": {
            "memory": {
                "status": "ok",
                "facts": {
                    "cgroup_memory_swap_max": swap_max,
                    "oomd_user_slice_kill": oomd,
                    # meminfo swap is NOT virtualized: 0 here on a HEALTHY
                    # container (verified live 2026-07-16) — the check must
                    # never key on it.
                    "swap_total": 0,
                },
            },
            "host_system": {"status": "ok", "facts": {"swap_total_kb": host_swap_kb}},
            "host_virt": {
                "status": "ok",
                "facts": {"container_limits": {"limits.memory.swap": knob}},
            },
            "network": {
                "status": "ok",
                "facts": {
                    "networkd_manages_default_route": networkd_route,
                    "networkd_default_route_keepconfig": keepconfig,
                    "network_watchdog_enabled": watchdog,
                },
            },
            "virt": {"status": "ok", "facts": {"container": container}},
            "storage": {"status": "ok", "facts": {"cc_tmp_isolated": cc_tmp_isolated}},
        }
    }
    if collected_at is not None:
        profile["collected_at"] = collected_at
    return profile


def _use_profile(monkeypatch, profile: dict) -> None:
    monkeypatch.setattr(_loop, "load_profile", lambda: profile)


@pytest.fixture(autouse=True)
def _reset_cooldown():
    _loop._last_infra_posture_alert_at = 0.0
    _loop._last_infra_posture_key = ""
    yield
    _loop._last_infra_posture_alert_at = 0.0
    _loop._last_infra_posture_key = ""


# ── rule semantics ──────────────────────────────────────────────────────────


_ALL_DEFECTS = dict(
    swap_max=0,
    oomd=False,
    host_swap_kb=0,
    knob="false",
    networkd_route=True,  # networkd owns the route, so the network rules apply
    keepconfig=False,
    watchdog=False,
    cc_tmp_isolated=False,
)


def test_all_defects_detected():
    assert _infra_missing_protections(_profile(**_ALL_DEFECTS)) == [
        "cc_tmp_shared_fs",
        "container_swap_disabled",
        "container_swap_knob_off",
        "host_swap_absent",
        "network_watchdog_absent",
        "networkd_keepconfig_missing",
        "oomd_pressure_kill_off",
    ]


def test_healthy_profile_no_defects():
    assert _infra_missing_protections(_profile()) == []


def test_cc_tmp_shared_fs_detected_in_lxc():
    # cc-tmp NOT isolated on an lxc container → the nag fires.
    assert "cc_tmp_shared_fs" in _infra_missing_protections(
        _profile(cc_tmp_isolated=False, container="lxc")
    )


def test_cc_tmp_isolated_is_silent():
    # cc-tmp on its own volume → no nag.
    assert "cc_tmp_shared_fs" not in _infra_missing_protections(_profile(cc_tmp_isolated=True))


def test_cc_tmp_not_nagged_off_lxc():
    # Non-container topology: the remedy (a dedicated incus volume) is not
    # actionable, so a False fact must stay silent.
    assert "cc_tmp_shared_fs" not in _infra_missing_protections(
        _profile(cc_tmp_isolated=False, container="none")
    )


def test_cc_tmp_fact_absent_is_silent():
    # Fact not yet collected (pre-bootstrap) → silent.
    prof = _profile(container="lxc")
    del prof["sections"]["storage"]["facts"]["cc_tmp_isolated"]
    assert "cc_tmp_shared_fs" not in _infra_missing_protections(prof)


def test_absent_facts_are_silent():
    # No guardian host plane, no host_virt, empty memory facts — a public
    # install missing optional planes must never false-alarm.
    assert _infra_missing_protections({"sections": {"memory": {"status": "ok", "facts": {}}}}) == []
    assert _infra_missing_protections({}) == []


def test_not_ok_section_facts_are_ignored():
    # Codex P2 (PR #1096): a per-section collector failure RETAINS the
    # previous facts (status=error/unavailable) while the top-level
    # collected_at still bumps — rules must never assert from those.
    profile = _profile(swap_max=0, oomd=False, host_swap_kb=0, knob="false")
    for plane in ("memory", "host_system", "host_virt"):
        profile["sections"][plane]["status"] = "error"
    assert _infra_missing_protections(profile) == []


def test_cgroup_v1_gates_oomd_rule():
    # swap knob unreadable (None = cgroup v1) → oomd pressure-kill cannot work
    # there, so oomd=False must not alert.
    assert _infra_missing_protections(_profile(swap_max=None, oomd=False)) == []


def test_bool_false_is_not_explicit_zero():
    # bool is an int subclass (False == 0); a malformed bool fact must not
    # read as "explicitly zero".
    assert "container_swap_disabled" not in _infra_missing_protections(_profile(swap_max=False))
    assert "host_swap_absent" not in _infra_missing_protections(_profile(host_swap_kb=False))


def test_nonzero_swap_limit_is_healthy():
    # A numeric limit is a SET limit, not the wedge state.
    assert _infra_missing_protections(_profile(swap_max=1_073_741_824)) == []


def test_absent_knob_is_healthy():
    # incus default is true — only an explicit "false" alerts.
    profile = _profile()
    del profile["sections"]["host_virt"]["facts"]["container_limits"]
    assert _infra_missing_protections(profile) == []


# ── network-plane rules (gated on networkd managing the default route) ───────


def test_network_defects_when_networkd_manages():
    # networkd owns the default route but neither protection is installed.
    missing = _infra_missing_protections(
        _profile(networkd_route=True, keepconfig=False, watchdog=False)
    )
    assert missing == ["network_watchdog_absent", "networkd_keepconfig_missing"]


def test_networkmanager_box_suppressed():
    # networkd does NOT manage the route (NetworkManager / foreign) — the
    # protections are not applicable, so False facts must stay silent. This is
    # the whole point of the disambiguation gate (no false-positive on a public
    # NetworkManager install).
    assert (
        _infra_missing_protections(_profile(networkd_route=False, keepconfig=False, watchdog=False))
        == []
    )


def test_networkd_gate_absent_suppressed():
    # An OLD profile collected before this fact existed: the network section is
    # ok but has no networkd_manages_default_route key → suppressed until the
    # next collection re-populates it (never assert from a missing gate).
    profile = _profile(keepconfig=False, watchdog=False)
    del profile["sections"]["network"]["facts"]["networkd_manages_default_route"]
    assert _infra_missing_protections(profile) == []


def test_network_gate_requires_bool_true():
    # Strict `is True` (mirrors the explicit-value discipline): a stringy
    # "true" is not the bool the collector emits, so it must not enable the
    # rules — a malformed fact fails safe (suppress), never false-alarms.
    assert (
        _infra_missing_protections(
            _profile(networkd_route="true", keepconfig=False, watchdog=False)
        )
        == []
    )


def test_network_protections_present_are_silent():
    assert _infra_missing_protections(_profile(networkd_route=True)) == []


def test_network_not_ok_section_is_ignored():
    # Codex P2 parity: a failing network collector RETAINS facts with
    # status=error — the rules must not assert from them.
    profile = _profile(networkd_route=True, keepconfig=False, watchdog=False)
    profile["sections"]["network"]["status"] = "error"
    assert _infra_missing_protections(profile) == []


# ── alert lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unprotected_profile_raises_high_alert(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0, oomd=False))
        await _check_infra_protection_posture(db)
        alerts = await _alerts(db)
        assert len(alerts) == 1
        assert alerts[0]["priority"] == "high"  # non-paging by design
        assert alerts[0]["resolved"] == 0
        assert "container_swap_disabled" in alerts[0]["content"]
        assert "oomd_pressure_kill_off" in alerts[0]["content"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_healthy_profile_is_silent(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile())
        await _check_infra_protection_posture(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_profile_is_silent(monkeypatch):
    # Pre-first-refresh (fresh install): load_profile() returns {}.
    db = await _setup()
    try:
        _use_profile(monkeypatch, {})
        await _check_infra_protection_posture(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_collected_at_is_silent(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0, collected_at=None))
        await _check_infra_protection_posture(db)
        assert await _alerts(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stable_state_keeps_one_open_row(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)
        # Same state within the cooldown → early return.
        await _check_infra_protection_posture(db)
        # Same state past the cooldown (simulated by reset) → atomic dedup
        # (source + content_hash + resolved=0) still keeps one row.
        _loop._last_infra_posture_alert_at = 0.0
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_posture_change_supersedes_old_row(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0, oomd=False))
        await _check_infra_protection_posture(db)
        # oomd healed, swap still bad — a NEW key bypasses the cooldown and
        # supersedes the old row: exactly one open row = the current state.
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)

        alerts = await _alerts(db)
        assert len(alerts) == 2
        open_rows = _open(alerts)
        assert len(open_rows) == 1
        assert "oomd_pressure_kill_off" not in open_rows[0]["content"]
        superseded = [a for a in alerts if a["resolved"] == 1]
        assert superseded[0]["resolution_notes"] == _loop._INFRA_POSTURE_SUPERSEDED_NOTE
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_resolves_alert(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1

        _use_profile(monkeypatch, _profile())
        await _check_infra_protection_posture(db)
        alerts = await _alerts(db)
        assert all(a["resolved"] == 1 for a in alerts)
        # Cooldown cleared → a re-degradation re-alerts promptly.
        assert _loop._last_infra_posture_alert_at == 0.0
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_profile_alerts_unknown_not_posture(monkeypatch):
    db = await _setup()
    try:
        # Defect facts present, but the profile is 10 days old — the refresh
        # pipeline is broken; posture must NOT be asserted from dead facts.
        _use_profile(monkeypatch, _profile(swap_max=0, age_days=10))
        await _check_infra_protection_posture(db)
        alerts = await _alerts(db)
        assert len(alerts) == 1
        assert "UNKNOWN" in alerts[0]["content"]
        assert "container_swap_disabled" not in alerts[0]["content"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_profile_supersedes_stale_alert(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(age_days=10))
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1

        # A fresh, healthy profile lands → the stale-UNKNOWN row resolves.
        _use_profile(monkeypatch, _profile())
        await _check_infra_protection_posture(db)
        assert _open(await _alerts(db)) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_none_db_and_resolve_are_noops():
    await _check_infra_protection_posture(None)  # must not raise
    await _resolve_infra_protection_posture(None)  # must not raise


@pytest.mark.asyncio
async def test_load_profile_error_never_raises(monkeypatch):
    db = await _setup()
    try:

        def _boom():
            raise OSError("disk gone")

        monkeypatch.setattr(_loop, "load_profile", _boom)
        await _check_infra_protection_posture(db)  # must not raise into the tick
        assert await _alerts(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unverifiable_plane_holds_resolve(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1

        # The memory collector starts failing: facts retained, status=error.
        # We can neither verify the defect nor the recovery — the alert must
        # HOLD (no false all-clear from stale facts), not resolve.
        broken = _profile(swap_max=0)
        broken["sections"]["memory"]["status"] = "error"
        _use_profile(monkeypatch, broken)
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unavailable_empty_section_does_not_block_resolve(monkeypatch):
    db = await _setup()
    try:
        _use_profile(monkeypatch, _profile(swap_max=0))
        await _check_infra_protection_posture(db)
        assert len(_open(await _alerts(db))) == 1

        # Guardian-less install: host planes permanently "unavailable" with
        # EMPTY facts. They never contributed a rule and must not hold a
        # container-plane recovery hostage.
        healed = _profile()
        for plane in ("host_system", "host_virt"):
            healed["sections"][plane]["status"] = "unavailable"
            healed["sections"][plane]["facts"] = {}
        _use_profile(monkeypatch, healed)
        await _check_infra_protection_posture(db)
        assert _open(await _alerts(db)) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_alert_notes_unverifiable_planes(monkeypatch):
    db = await _setup()
    try:
        # A real defect in an ok section still alerts even while another
        # plane is unverifiable — and the content names the failing plane.
        profile = _profile(swap_max=0)
        profile["sections"]["host_system"]["status"] = "error"
        _use_profile(monkeypatch, profile)
        await _check_infra_protection_posture(db)
        open_rows = _open(await _alerts(db))
        assert len(open_rows) == 1
        assert "host_system" in open_rows[0]["content"]
    finally:
        await db.close()


# ── coverage guardrails (provision-or-surface convention) ──────────────────


def test_every_rule_slug_has_detail_text():
    # The alert content is built via _INFRA_POSTURE_DETAIL[slug] — a rule slug
    # without detail text would KeyError inside the (swallowed) check. Derive
    # the producible set from the rules themselves.
    producible = set(_infra_missing_protections(_profile(**_ALL_DEFECTS)))
    assert producible == set(_loop._INFRA_POSTURE_DETAIL)


def test_resilience_facts_are_covered():
    # Provision-or-surface: every memory- AND network-resilience effective-fact
    # must be read by the posture rules, so a protection can't silently lose its
    # surfacing signal in a refactor. (Adding a NEW protection fact requires
    # extending both the rules and this list — that is the point.)
    src = inspect.getsource(_infra_missing_protections)
    for fact in (
        "cgroup_memory_swap_max",
        "oomd_user_slice_kill",
        "swap_total_kb",
        "limits.memory.swap",
        "networkd_manages_default_route",
        "networkd_default_route_keepconfig",
        "network_watchdog_enabled",
        "cc_tmp_isolated",
    ):
        assert fact in src, f"posture rules no longer read {fact!r}"


def test_check_is_wired_into_the_tick():
    # Level-3 wiring proof: the check must have a live call site in the tick
    # pipeline, not just exist as a function.
    src = inspect.getsource(_loop)
    assert "await _check_infra_protection_posture(self._db)" in src
