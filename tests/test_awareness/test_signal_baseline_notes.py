"""Regression: signal ``baseline_note`` strings must accurately describe what the
collector measures.

``baseline_note`` is fed VERBATIM into the reflection LLM prompt
(``awareness/signal_format.py`` appends ``" -- baseline: {note}"``), so a stale or
misleading note directly causes wrong reflections. A stale ``critical_failure`` note
("provider is down") — describing an old circuit-breaker placeholder rather than the
DB/Qdrant/Ollama health probes the collector actually runs — made a live Micro
reflection falsely alert "a provider is down." These tests pin the accuracy of every
note this audit corrected, each written to FAIL against the pre-fix strings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from genesis.awareness.signals import ContainerMemoryCollector, SchedulerLivenessCollector
from genesis.awareness.types import SignalReading
from genesis.learning.signals.critical_failure import CriticalFailureCollector
from genesis.learning.signals.pending_items import PendingItemCollector
from genesis.learning.signals.sentinel_activity import SentinelActivityCollector


async def test_critical_failure_note_describes_health_probes_not_providers():
    # The collector runs DB/Qdrant/Ollama health probes, NOT LLM-provider circuit
    # breakers. The note must not say "provider" (which primed a false "provider down"
    # reflection). Empty probes → collect() still returns the note at value 0.0.
    reading = await CriticalFailureCollector([]).collect()
    note = reading.baseline_note or ""
    # The old (stale) note framed this as LLM-provider reachability. Ban those
    # misleading affirmatives; a clarifying "not an LLM-provider outage" is fine.
    assert "providers reachable" not in note.lower(), note
    assert "provider down" not in note.lower(), note
    assert any(k in note.lower() for k in ("probe", "qdrant", "ollama")), note
    # Must distinguish local infra/service health from CLOUD LLM-provider status
    # (Ollama, when enabled, IS a local LLM provider whose DOWN fires this signal).
    assert "cloud" in note.lower(), note


async def test_container_memory_note_says_excludes_cache_not_includes():
    # The collector reads anon+kernel only and its own docstring says it EXCLUDES
    # reclaimable page cache (which inflates memory.current and causes false pressure).
    # The note must not claim the opposite ("Includes page cache").
    import genesis.autonomy.watchdog as watchdog_mod

    # patched at call time (collect imports it lazily) → hermetic vs host cgroup layout
    orig = watchdog_mod.get_container_anon_memory
    watchdog_mod.get_container_anon_memory = lambda: (1024, 4096)
    try:
        reading = await ContainerMemoryCollector().collect()
    finally:
        watchdog_mod.get_container_anon_memory = orig
    note = reading.baseline_note or ""
    assert "includes page cache" not in note.lower(), note
    assert "exclud" in note.lower(), f"note should say it excludes cache: {note!r}"


async def test_sentinel_note_documents_awaiting_approval_state(tmp_path):
    # _STATE_VALUES maps awaiting_dispatch/action_approval -> 0.5; the note omitted 0.5
    # entirely, leaving the LLM no grounding for "blocked on the user."
    state = tmp_path / "sentinel_state.json"
    state.write_text(json.dumps({"current_state": "awaiting_dispatch_approval"}))
    reading = await SentinelActivityCollector(state_path=state).collect()
    assert reading.value == 0.5  # sanity: the state maps to 0.5
    note = reading.baseline_note or ""
    assert "0.5" in note, note
    assert "approval" in note.lower(), note


async def test_pending_items_note_covers_missing_or_corrupt_state(db):
    # value==1.0 also fires on a missing/corrupt cognitive_state row
    # (no_cognitive_state / db_error / bad_timestamp), NOT only 7+ day staleness.
    reading = await PendingItemCollector(db).collect()
    # empty (or absent) cognitive_state -> 1.0 via no_cognitive_state/db_error
    assert reading.value == 1.0
    note = reading.baseline_note or ""
    assert "cognitive_state" in note.lower() or "missing" in note.lower(), note


def _fake_runtime(job_health: dict):
    class _RT:
        _bootstrap_completed_at = None  # skip the bootstrap grace branch
        _surplus_scheduler = object()  # non-None → run the surplus liveness check

        def __init__(self, jh):
            self.job_health = jh

    return _RT(job_health)


async def test_scheduler_liveness_firing_branch_has_baseline_note():
    # The FIRING branch (stale surplus jobs) previously attached NO baseline_note —
    # exactly when the signal matters, the LLM got zero grounding. It must have one.
    stale = (datetime.now(UTC) - timedelta(seconds=2000)).isoformat()  # > 900s threshold
    collector = SchedulerLivenessCollector(
        runtime=_fake_runtime({"surplus_dispatch": {"last_run": stale}})
    )
    reading = await collector.collect()
    assert reading.value > 0.0, "expected the firing (stale) branch"
    assert reading.baseline_note, "firing branch must carry a baseline_note"
    note = (reading.baseline_note or "").lower()
    assert "surplus" in note
    # The note must not point the LLM at metadata — signal_format.py never renders it.
    assert "metadata" not in note, note
    # Default threshold (900s) -> "15+ min".
    assert "15+ min" in note, note


async def test_scheduler_liveness_firing_note_derives_threshold_from_config():
    # The duration in the note must derive from the configured stale_threshold_s, not
    # a hardcoded "15 min" — else a reconfigured collector feeds the LLM a wrong duration.
    stale = (datetime.now(UTC) - timedelta(seconds=2000)).isoformat()
    collector = SchedulerLivenessCollector(
        runtime=_fake_runtime({"surplus_dispatch": {"last_run": stale}}),
        stale_threshold_s=120,  # 2 min
    )
    reading = await collector.collect()
    note = (reading.baseline_note or "").lower()
    assert "2+ min" in note, note
    assert "15+ min" not in note, note


async def test_scheduler_liveness_healthy_note_scopes_to_surplus_only():
    # The healthy-branch note must make clear it checks ONLY the surplus scheduler,
    # not the awareness loop's own scheduling.
    fresh = datetime.now(UTC).isoformat()
    collector = SchedulerLivenessCollector(
        runtime=_fake_runtime({"surplus_dispatch": {"last_run": fresh}})
    )
    reading = await collector.collect()
    assert reading.value == 0.0
    assert isinstance(reading, SignalReading)
    assert "only" in (reading.baseline_note or "").lower(), reading.baseline_note
