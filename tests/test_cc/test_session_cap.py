"""Unit tests for the capacity-based cc session cap (genesis.cc.session_cap).

The load-bearing test is `test_collapse_case_now_allows` — the exact live scenario
that locked the operator out (3 running, 6566MB free → old formula computed cap 2).
Under the capacity model it must ALLOW.
"""

from __future__ import annotations

import pytest

from genesis.cc.session_cap import CapConfig, Decision, classify_origin, decide

# Reference box: 18GB, 6 cores. Defaults → SAFE_CAP = (18432-4096)//3072 = 4.
CFG = CapConfig()
TS = "100.100.100.100 56002 100.100.0.1 22"  # tailscale operator
LAN = "192.0.2.10 51000 192.0.2.1 22"  # LAN operator (RFC 5737 TEST-NET, synthetic)
PUB = "8.8.8.8 40000 203.0.113.1 22"  # public remote


def _d(existing, ssh, *, total=18432, avail=6566, cpu=6, cfg=CFG) -> Decision:
    return decide(
        mem_total_mb=total,
        mem_available_mb=avail,
        existing=existing,
        cpu_count=cpu,
        ssh_connection=ssh,
        config=cfg,
    )


# ── SAFE_CAP shape ────────────────────────────────────────────────────────────
def test_safe_cap_is_four_on_reference_box():
    assert _d(0, TS).safe_cap == 4


def test_safe_cap_scales_up_on_a_bigger_box():
    # 32GB, 16 cores → (32768-4096)//3072 = 9 (cpu clamp does not bind at 16).
    assert _d(0, TS, total=32768, cpu=16).safe_cap == 9


def test_safe_cap_clamped_by_cpu_count():
    # RAM would allow 9 but only 6 cores → clamp to 6 (thrash guard).
    assert _d(0, TS, total=32768, cpu=6).safe_cap == 6


def test_tiny_box_never_locks_out_floor_of_one():
    # 4GB: (4096-4096)//3072 = 0 → floored to 1.
    assert _d(0, TS, total=4096, avail=3000).safe_cap == 1


# ── THE BUG: collapse case must now ALLOW ─────────────────────────────────────
def test_collapse_case_now_allows():
    # 3 running, 6566MB free, tailscale operator — the exact lockout scenario.
    d = _d(3, TS)
    assert d.action == "ALLOW"
    assert d.safe_cap == 4


def test_safe_cap_is_stable_as_free_ram_drops():
    # Free RAM varying from 6566 down to 2000 does NOT change SAFE_CAP (no collapse).
    caps = {_d(3, TS, avail=a).safe_cap for a in (6566, 5000, 3000, 2000)}
    assert caps == {4}


# ── Emergency slot (operator origin, +1 above SAFE_CAP) ───────────────────────
def test_operator_gets_emergency_slot_at_cap():
    d = _d(4, TS)  # existing == safe_cap → the +1 emergency slot
    assert d.action == "ALLOW"
    assert d.reason == "emergency_slot"
    assert d.effective_cap == 5


@pytest.mark.parametrize("ssh", [TS, LAN])
def test_both_lan_and_tailscale_are_operator(ssh):
    assert _d(4, ssh).action == "ALLOW"


# ── Normal method (dashboard/public) is held to SAFE_CAP ──────────────────────
def test_normal_origin_denied_over_cap():
    assert _d(4, None).action == "DENY"  # dashboard/manual (no SSH_CONNECTION)


def test_public_ssh_is_not_operator():
    assert _d(4, PUB).action == "DENY"


def test_normal_origin_allowed_under_cap():
    assert _d(2, None).action == "ALLOW"


def test_normal_origin_denied_when_ram_tight_under_cap():
    d = _d(2, None, avail=800)  # under cap but below OOM floor
    assert d.action == "DENY"
    assert d.reason == "oom_floor"


# ── Operator is NEVER hard-denied → RECLAIM instead ───────────────────────────
def test_operator_over_emergency_cap_reclaims_not_denies():
    d = _d(5, TS)  # past safe_cap(4) + emergency(1)
    assert d.action == "RECLAIM"
    assert d.reason == "cap_full"


def test_operator_ram_tight_reclaims_not_denies():
    d = _d(2, TS, avail=800)  # under cap but RAM below floor
    assert d.action == "RECLAIM"
    assert d.reason == "oom_floor"


def test_operator_never_returns_deny_across_the_grid():
    # Exhaustive: no (existing, avail) combination yields DENY for an operator.
    actions = {_d(e, TS, avail=a).action for e in range(0, 8) for a in (400, 800, 1536, 3000, 6566)}
    assert "DENY" not in actions


# ── Emergency lever can be disabled via config ────────────────────────────────
def test_emergency_slots_zero_still_reclaims_never_denies_operator():
    cfg = CapConfig(emergency_slots=0)
    d = _d(4, TS, cfg=cfg)  # at cap, no emergency slot → reclaim (still not deny)
    assert d.action == "RECLAIM"
    assert d.effective_cap == 4


# ── Origin classification ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "client,expected",
    [
        ("100.100.100.100", "tailscale"),  # CGNAT
        ("100.64.0.1", "tailscale"),  # CGNAT low boundary
        ("100.127.255.254", "tailscale"),  # CGNAT high boundary
        ("100.63.0.1", "public"),  # just below CGNAT
        ("100.128.0.1", "public"),  # just above CGNAT
        ("198.51.100.5", "lan"),  # RFC 5737 TEST-NET-2 → is_private
        ("10.0.0.5", "lan"),  # RFC1918 10/8
        ("172.16.0.5", "lan"),  # RFC1918 172.16/12
        ("127.0.0.1", "lan"),  # loopback = local operator
        ("8.8.8.8", "public"),
        ("fd7a:115c:a1e0::1234", "tailscale"),  # tailscale's fixed tailnet ULA prefix
        ("fd00:1:2:3::1", "lan"),  # generic ULA (fc00::/7)
        ("garbage", "public"),  # unparseable → non-operator (safe)
    ],
)
def test_classify_origin(client, expected):
    ssh = f"{client} 51000 100.100.0.1 22"
    assert classify_origin(ssh) == expected


def test_classify_origin_none_when_no_ssh():
    assert classify_origin(None) == "none"
    assert classify_origin("") == "none"
    assert classify_origin("   ") == "none"


# ── Config env parsing (guards against nonsensical overrides) ─────────────────
def test_config_from_env_guards_zero_per_session():
    cfg = CapConfig.from_env({"GENESIS_CC_PER_SESSION_MB": "0"})
    assert cfg.per_session_mb == 3072  # zero rejected → default (no ZeroDivision)


def test_config_from_env_reads_overrides():
    cfg = CapConfig.from_env(
        {
            "GENESIS_CC_SYSTEM_RESERVE_MB": "6000",
            "GENESIS_CC_PER_SESSION_MB": "2000",
            "GENESIS_CC_OOM_FLOOR_MB": "1000",
            "GENESIS_CC_EMERGENCY_SLOTS": "2",
        }
    )
    assert (cfg.system_reserve_mb, cfg.per_session_mb, cfg.oom_floor_mb, cfg.emergency_slots) == (
        6000,
        2000,
        1000,
        2,
    )
