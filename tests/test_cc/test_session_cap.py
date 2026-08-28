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
        # Tailscale's v6 is a ULA (fc00::/7) → classified operator via is_private
        # ("lan"), no explicit tailnet range hardcoded. A generic ULA proves it:
        ("fd00:1:2:3::1", "lan"),  # ULA (fc00::/7) → operator
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


@pytest.mark.parametrize(
    "client,expected",
    [
        ("::ffff:100.100.100.100", "tailscale"),  # IPv4-mapped CGNAT → operator
        ("::ffff:192.0.2.10", "lan"),  # IPv4-mapped RFC5737 (is_private) → operator
        ("::ffff:8.8.8.8", "public"),  # IPv4-mapped public → non-operator
    ],
)
def test_classify_origin_unwraps_ipv4_mapped(client, expected):
    # A dual-stack sshd can report ::ffff:a.b.c.d; without unwrapping, is_private
    # is False on the mapped v6 form and a tailscale/LAN operator is misread public.
    assert classify_origin(f"{client} 51000 100.100.0.1 22") == expected


# ── Config env parsing (guards against nonsensical overrides) ─────────────────
def test_config_from_env_guards_zero_per_session():
    cfg = CapConfig.from_env({"GENESIS_CC_PER_SESSION_MB": "0"})
    assert cfg.per_session_mb == 3072  # zero rejected → default (no ZeroDivision)


# Reserve's default (4096) differs from every override below, so a reject is
# unambiguous (no false pass from a default that happens to equal the input).
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("6000", 6000),  # plain digits → accepted
        (" 6000 ", 6000),  # surrounding whitespace stripped → accepted
        ("+6000", 4096),  # sign rejected → default
        ("6000mb", 4096),  # unit suffix rejected → default
        ("6_000", 4096),  # python int() underscore rejected → default
        ("06000", 4096),  # leading zero (bash octal hazard) rejected → default
        ("0x10", 4096),  # hex rejected → default
        ("-5", 4096),  # negative rejected → default
        ("99999999", 4096),  # 8 digits (bash-overflow guard, >7) rejected → default
    ],
)
def test_config_from_env_matches_bash_grammar(raw, expected):
    # F8: python accepts ONLY the bash fallback's grammar so the same cc-slot.env
    # yields the same cap on the primary and the degraded static path.
    assert CapConfig.from_env({"GENESIS_CC_SYSTEM_RESERVE_MB": raw}).system_reserve_mb == expected


def test_config_from_env_emergency_grammar():
    # 0 explicitly disables; malformed/oversized → default (1), matching bash.
    assert CapConfig.from_env({"GENESIS_CC_EMERGENCY_SLOTS": "0"}).emergency_slots == 0
    assert CapConfig.from_env({"GENESIS_CC_EMERGENCY_SLOTS": "2"}).emergency_slots == 2
    assert CapConfig.from_env({"GENESIS_CC_EMERGENCY_SLOTS": "-1"}).emergency_slots == 1  # default
    assert (
        CapConfig.from_env({"GENESIS_CC_EMERGENCY_SLOTS": "100"}).emergency_slots == 1
    )  # >99 → default
    assert (
        CapConfig.from_env({"GENESIS_CC_EMERGENCY_SLOTS": "07"}).emergency_slots == 1
    )  # leading zero → default


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


# ── OOM floor must cover a full per-session footprint (Codex P1) ──────────────
def test_operator_below_one_full_session_reclaims_not_allows():
    # avail (2000) is above the old 1536 floor but BELOW one session (3072) → a
    # started session could grow past all free RAM → RECLAIM, never ALLOW.
    d = _d(1, TS, avail=2000)
    assert d.action == "RECLAIM"
    assert d.reason == "oom_floor"


def test_normal_below_one_full_session_denies():
    d = _d(1, None, avail=2000)
    assert d.action == "DENY"
    assert d.reason == "oom_floor"


def test_oom_message_reports_the_real_threshold_not_just_floor():
    # F6: with defaults the effective threshold is max(per=3072, floor=1536)=3072;
    # the message must print 3072, not the bare oom_floor (1536) — 2000MB free is
    # denied precisely because it's under 3072, so the number the operator reads
    # has to match the gate that fired.
    d_norm = _d(1, None, avail=2000)  # normal → DENY
    d_op = _d(1, TS, avail=2000)  # operator → RECLAIM
    assert "need >= 3072MB" in d_norm.message
    assert "need >= 3072MB" in d_op.message
    assert "1536" not in d_norm.message and "1536" not in d_op.message


def test_allow_needs_room_for_a_full_session():
    # Exactly one session's worth free (3072) → ALLOW; one MB less → not ram_ok.
    assert _d(1, TS, avail=3072).action == "ALLOW"
    assert _d(1, TS, avail=3071).action == "RECLAIM"


# ── Container/cgroup-aware memory (Codex P1: procfs may show HOST values) ─────
def test_effective_memory_caps_by_cgroup(monkeypatch):
    import genesis.cc.session_cap as m
    from genesis.runtime import cgroup

    monkeypatch.setattr(m, "read_meminfo", lambda *a, **k: (32768, 30000))  # host-inflated
    monkeypatch.setattr(cgroup, "read_container_memory_max", lambda: 16 * 1024**3)  # 16 GiB
    monkeypatch.setattr(
        cgroup, "read_container_memory_current", lambda: 10 * 1024**3
    )  # 10 GiB used
    monkeypatch.setattr(
        cgroup, "read_container_memory_reclaimable", lambda: 2 * 1024**3
    )  # 2 GiB cache
    total, avail, known = m.effective_memory()
    assert total == 16384  # capped to the cgroup limit, not host 32768
    # available = limit(16) - usage(10) + reclaimable cache(2) = 8 GiB, not host 30000
    assert avail == 8 * 1024
    assert known is True


def test_effective_memory_procfs_only_when_no_cgroup(monkeypatch):
    import genesis.cc.session_cap as m
    from genesis.runtime import cgroup

    monkeypatch.setattr(m, "read_meminfo", lambda *a, **k: (8192, 4000))
    monkeypatch.setattr(cgroup, "read_container_memory_max", lambda: None)  # unlimited/unreadable
    assert m.effective_memory() == (8192, 4000, True)


def test_effective_memory_unknown_when_usage_unreadable(monkeypatch):
    # Codex P2: a FINITE cgroup limit but unreadable memory.current must NOT leave
    # host procfs MemAvailable standing (it may describe host headroom → over-allow).
    # total is still clamped to the limit; availability is flagged unknown so main()
    # substitutes the conservative model estimate.
    import genesis.cc.session_cap as m
    from genesis.runtime import cgroup

    monkeypatch.setattr(m, "read_meminfo", lambda *a, **k: (32768, 30000))  # host-inflated
    monkeypatch.setattr(cgroup, "read_container_memory_max", lambda: 16 * 1024**3)
    monkeypatch.setattr(cgroup, "read_container_memory_current", lambda: None)  # unreadable
    monkeypatch.setattr(cgroup, "read_container_memory_reclaimable", lambda: None)
    total, avail, known = m.effective_memory()
    assert total == 16384  # limit still applied
    assert avail == 30000  # raw value passes through …
    assert known is False  # … but flagged untrustworthy (main() will not use it)


def test_main_degrades_available_to_model_estimate_when_usage_unknown(monkeypatch, capsys):
    # End-to-end: usage-unknown → main() computes free from the capacity model
    # (total − reserve − existing×per), not host procfs, and the gate acts on THAT.
    # 16384 − 4096 − 3×3072 = 3072 → exactly one session's room → operator ALLOW.
    import genesis.cc.session_cap as m

    monkeypatch.setattr(m, "effective_memory", lambda: (16384, 30000, False))
    monkeypatch.setattr(m, "_cpu_count", lambda: 6)
    monkeypatch.setenv("SSH_CONNECTION", TS)
    rc = m.main(["--existing", "3"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines()[0] == "ALLOW"


def test_effective_memory_v2_reclaimable_excludes_shmem(monkeypatch):
    # F5: v2 reclaimable = inactive_file + active_file (file LRU), NOT the `file`
    # type-counter (which also counts tmpfs/shmem on the anon LRU → over-states).
    from genesis.runtime import cgroup

    monkeypatch.setattr(
        cgroup,
        "_read_text",
        lambda p: (
            "file 5000\ninactive_file 2000\nactive_file 1800\nshmem 1200"
            if p == "/sys/fs/cgroup/memory.stat"
            else None
        ),
    )
    assert cgroup.read_container_memory_reclaimable() == 3800  # 2000+1800, not 5000


# ── cgroup v1 fallback (older hosts lack the v2 unified paths) ────────────────
def test_cgroup_v1_memory_max_fallback(monkeypatch):
    from genesis.runtime import cgroup

    # v2 memory.max absent → read v1 memory.limit_in_bytes.
    monkeypatch.setattr(
        cgroup,
        "_read_text",
        lambda p: "16000000000" if p.endswith("memory/memory.limit_in_bytes") else None,
    )
    assert cgroup.read_container_memory_max() == 16000000000


def test_cgroup_v1_unlimited_sentinel_is_none(monkeypatch):
    from genesis.runtime import cgroup

    monkeypatch.setattr(
        cgroup,
        "_read_text",
        lambda p: "9223372036854771712" if "limit_in_bytes" in p else None,
    )
    assert cgroup.read_container_memory_max() is None  # sentinel → unlimited


def test_cgroup_v1_reclaimable_fallback(monkeypatch):
    from genesis.runtime import cgroup

    def fake(path):
        if path.endswith("memory/memory.stat"):  # v1 stat
            return "total_inactive_file 1000\ntotal_active_file 500\nrss 200"
        return None  # no v2 stat

    monkeypatch.setattr(cgroup, "_read_text", fake)
    assert cgroup.read_container_memory_reclaimable() == 1500
