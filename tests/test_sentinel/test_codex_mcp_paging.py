"""The paging decision requires stable, repeated release evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from genesis.sentinel import codex_mcp_inventory, codex_mcp_paging
from genesis.sentinel.codex_mcp_paging import Eligibility, RetentionTracker

THRESHOLD = 4096


def sample(threads, groups, *, status="complete", daemon=("boot", 10, 100), ram=5000):
    return SimpleNamespace(
        status=status, daemon=daemon, threads=frozenset(threads),
        groups=frozenset((n, n * 10) for n in groups), held_kib=ram * 1024,
    )


def perform_cycle(tracker, start, retained_groups, *, ram=5000):
    tracker.observe(sample("ab", (*retained_groups, 99)), start)
    tracker.observe(sample("a", retained_groups, ram=ram), start + 60)
    tracker.observe(sample("a", retained_groups, ram=ram), start + 120)
    tracker.observe(sample("a", retained_groups, ram=ram), start + 180)


def test_two_growing_unload_cycles_are_required():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    perform_cycle(tracker, 60, (1, 2))
    assert tracker.eligibility(240, THRESHOLD) == Eligibility.INELIGIBLE
    perform_cycle(tracker, 300, (1, 2, 3))
    assert tracker.eligibility(480, THRESHOLD) == Eligibility.ELIGIBLE
    assert tracker.eligibility(601, THRESHOLD) == Eligibility.UNKNOWN


def test_baseline_high_and_normal_unloads_do_not_request_activation():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1, 2, 3)), 0)
    perform_cycle(tracker, 60, (1, 2, 3))
    perform_cycle(tracker, 300, (1, 2, 3))
    assert tracker.eligibility(480, THRESHOLD) == Eligibility.INELIGIBLE


def test_group_first_seen_after_release_is_not_retention_evidence():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    tracker.observe(sample("ab", (1, 99)), 60)
    tracker.observe(sample("a", (1, 2)), 120)
    tracker.observe(sample("a", (1, 2)), 180)
    tracker.observe(sample("a", (1, 2)), 240)
    assert tracker.excursions == 0
    assert tracker.eligibility(240, THRESHOLD) == Eligibility.INELIGIBLE


def test_group_already_visible_before_expansion_is_not_new_growth():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    tracker.observe(sample("a", (1, 2)), 60)
    perform_cycle(tracker, 120, (1, 2))
    assert tracker.excursions == 0


def test_interrupted_return_cannot_join_two_separate_growth_cycles():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    perform_cycle(tracker, 60, (1, 2))
    tracker.observe(sample("ab", (1, 2, 98)), 300)
    tracker.observe(sample("a", (1, 2)), 360)
    perform_cycle(tracker, 390, (1, 2, 3))
    assert tracker.excursions == 1
    assert tracker.eligibility(570, THRESHOLD) == Eligibility.INELIGIBLE


def test_active_growth_cannot_raise_the_post_unload_reading():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,), ram=2000), 0)
    perform_cycle(tracker, 60, (1, 2), ram=2200)
    perform_cycle(tracker, 300, (1, 2, 3), ram=2500)
    tracker.observe(sample("ab", (1, 2, 3, 5), ram=5000), 540)
    assert tracker.excursions == 2
    assert tracker.eligibility(540, THRESHOLD) == Eligibility.INELIGIBLE


def test_latest_baseline_low_memory_invalidates_an_older_high_reading():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    perform_cycle(tracker, 60, (1, 2))
    perform_cycle(tracker, 300, (1, 2, 3))
    assert tracker.eligibility(480, THRESHOLD) == Eligibility.ELIGIBLE
    tracker.observe(sample("a", (1, 2, 3), ram=2000), 540)
    tracker.observe(sample("ab", (1, 2, 3, 4), ram=5000), 600)
    assert tracker.eligibility(600, THRESHOLD) == Eligibility.INELIGIBLE


def test_partial_observation_and_daemon_replacement():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    perform_cycle(tracker, 60, (1, 2))
    perform_cycle(tracker, 300, (1, 2, 3))
    tracker.observe(sample("", (), status="partial"), 540)
    assert tracker.eligibility(540, THRESHOLD) == Eligibility.UNKNOWN
    tracker.observe(sample("a", (1, 2, 3), daemon=("boot", 10, 101)), 600)
    assert tracker.eligibility(600, THRESHOLD) == Eligibility.INELIGIBLE


async def test_inventory_emits_shadow_decision_from_real_tracker(monkeypatch):
    observed = codex_mcp_inventory.Inventory(
        "2026-01-01T00:00:00+00:00", "complete", daemon=("boot", 10, 100),
        threads=frozenset({"one"}), groups=frozenset({(1, 10)}),
        held_kib=3000 * 1024, scan_held_kib=THRESHOLD * 1024, scan_complete=True,
    )
    monkeypatch.setattr(codex_mcp_inventory, "collect", AsyncMock(return_value=observed))
    monkeypatch.setattr(codex_mcp_inventory, "_latest", None)
    monkeypatch.setattr(codex_mcp_paging, "_tracker", RetentionTracker())
    monkeypatch.setattr(codex_mcp_inventory.codex_mcp_stop, "load_mode", lambda: "propose_only")
    monkeypatch.setattr(codex_mcp_inventory.codex_mcp_stop, "load_propose_at_mb", lambda: THRESHOLD)
    monkeypatch.delenv("GENESIS_CODEX_MCP_INVENTORY_DISABLED", raising=False)
    bus = AsyncMock()

    await codex_mcp_inventory.sample(bus)

    bus.emit.assert_awaited_once()
    assert bus.emit.call_args.kwargs["paging"] == {
        "eligibility": "ineligible",
        "threshold_mb": THRESHOLD,
        "legacy_threshold_crossed": True,
        "completed_growth_cycles": 0,
        "baseline_groups": 1,
        "baseline_held_kib": 3000 * 1024,
        "confirmed_held_kib": 0,
        "residual_groups": 0,
    }


async def test_inventory_records_an_unverified_control_socket(monkeypatch):
    observed = codex_mcp_inventory.Inventory("2026-01-01T00:00:00+00:00", "unavailable")
    monkeypatch.setattr(codex_mcp_inventory, "collect", AsyncMock(return_value=observed))
    monkeypatch.setattr(codex_mcp_inventory, "_latest", None)
    monkeypatch.setattr(codex_mcp_paging, "_tracker", RetentionTracker())
    monkeypatch.setattr(codex_mcp_inventory.codex_mcp_stop, "load_mode", lambda: "propose_only")
    monkeypatch.setattr(codex_mcp_inventory.codex_mcp_stop, "load_propose_at_mb", lambda: THRESHOLD)
    monkeypatch.delenv("GENESIS_CODEX_MCP_INVENTORY_DISABLED", raising=False)
    bus = AsyncMock()

    await codex_mcp_inventory.sample(bus)

    assert bus.emit.call_args.kwargs["status"] == "unavailable"
    assert bus.emit.call_args.kwargs["paging"]["legacy_threshold_crossed"] is None
    assert bus.emit.call_args.kwargs["paging"]["eligibility"] == "unknown"


def test_collector_gap_breaks_an_unfinished_excursion():
    tracker = RetentionTracker()
    tracker.observe(sample("a", (1,)), 0)
    tracker.observe(sample("ab", (1, 2)), 60)
    tracker.observe(sample("a", (1, 2)), 120)
    tracker.observe(sample("a", (1, 2)), 180)
    tracker.observe(sample("a", (1, 2)), 360)
    assert tracker.excursions == 0
    assert tracker.baseline_groups == sample("a", (1, 2)).groups
