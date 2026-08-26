"""Backfill ``observations.origin_class`` for pre-provenance rows (WS-3).

``origin_class`` was added by migration 0057 but never backfilled, so every row
written before the write-boundary chokepoint (feat: observation write-boundary
origin provenance) carries ``origin_class IS NULL``. The read side is being
switched to treat NULL as external (fail-closed), so without this backfill the
laundering-critical surfaces (essential_knowledge L1, the reflection pipeline)
would drop ALL history. This stamps a definite origin where one can be derived,
and DELIBERATELY leaves genuinely-unknown sources NULL (fail-closed → excluded,
cosmetic, never a leak).

Two derivation paths, mirroring :func:`derive_observation_origin` minus the live
session-env (which is meaningless at migration time):

1. ``source = 'session:<cc_session_id>'`` → inherit the session's own stored
   ``cc_sessions.origin_class`` (JOIN). A session with NULL origin (foreground /
   pre-substrate) stays NULL → fail-closed.
2. every other source → :func:`_classify_source_snapshot`, a FROZEN in-file copy
   of the env-free classifier (recon/email_recon → external; the ``intake:*``
   split; the curated first-party allowlist; everything else → None). Frozen (not
   an import of the live classifier) so this one-time backfill is DETERMINISTIC
   across deployment history — see the snapshot note on that function.

Idempotent: every UPDATE is guarded ``WHERE origin_class IS NULL``. No commit —
the runner owns the transaction.

BOUNDED RESIDUAL (accepted, WS-3): path 2 trusts a historical row's OWN ``source``
label, which the ``observation_write`` MCP tool lets a caller set freely. So a
pre-0057 row that an external session forged with a first-party ``source`` would
backfill first_party. This is bounded and accepted rather than closed: (a) it
affects ONLY rows written before 0057 added the column (2026-07-14) — no
provenance was READ before this PR, so there was no pre-existing gate to evade for
gain; (b) such rows are >5 weeks old and TTL-expired out of the 7–14d surfacing
windows; (c) going FORWARD every write is stamped at the chokepoint from the live
session env, which a forged source cannot override. Closing the historical case
would require per-row content forensics with no signal to key on; the forward
invariant is the real fix. ``retrospective``/``cc_debrief`` are deliberately NOT
snapshot-classified here (their origin is the analyzed session's channel, stamped
live at the write site) — historical rows stay NULL → fail-closed excluded.
"""

from __future__ import annotations

import aiosqlite

# ── FROZEN source→origin snapshot (2026-08-22) ──────────────────────────────
# Self-contained per the migration convention (see 0077): a one-time backfill
# must be DETERMINISTIC across deployment history, so it snapshots the classifier
# instead of importing the live (evolving) memory.provenance._origin_from_source.
# Mirrors that classifier as of this migration; later classifier changes do NOT
# retroactively alter what 0085 assigns.
_SNAP_EXTERNAL = frozenset({"recon", "email_recon"})
_SNAP_FIRST_PARTY = frozenset(
    {
        "awareness_loop",
        "reflection",
        "deep_reflection",
        "strategic_reflection",
        "cc_reflection_light",
        "cc_reflection_strategic",
        "cc_reflection_deep",
        "dream_cycle",
        "genesis_version",
        "cc_version",
        "auto_memory_harvest",
        "post_commit_hook",
        "entity_adjudication",
        "process_reaper",
        "cc_memory_staleness",
        "infra_profile",
        "deploy_staleness_monitor",
        "quality_calibration",
        "weekly_assessment",
        "outreach_recovery",
        "genesis_ego",
        "ego_cycle",
        "ego_dispatch",
        "routing",
        "guardian",
        "sentinel",
        "skill_evolution",
        "skill_evolution_gate",
        "research_evaluation",
        "memory_integrity_posture_monitor",
        "infra_protection_posture_monitor",
        "duplicate_session_monitor",
        "user_model_staleness_monitor",
        "cc_login_monitor",
        "architect_triage",
        "surplus_promotion",
        "bootstrap",
        "cc_cap_monitor",
        "cc_invoker",
        "cc_slot_monitor",
        "cognitive_ledger",
        "dead_letter_monitor",
        "dead_letter_storm",
        "embedding_backlog_monitor",
        "extraction_calibration",
        "foreground_reaper",
        "git_health_monitor",
        "goal_cascade",
        "infrastructure_monitor",
        "nodatacow_monitor",
        "pid_budget_monitor",
        "procedure_rebuild",
        "settings_guard",
        "stability_monitor",
        "surplus_monitor",
        "surplus_scheduler",
        "task_executor",
        "wal_health_monitor",
        "follow_up_watchdog",
    }
)
# intake:<suffix> split — crawled-external vs Genesis-authored surplus.
_SNAP_INTAKE_PREFIX = "intake:"
_SNAP_INTAKE_EXTERNAL = frozenset(
    {
        "email_recon",
        "model_intelligence",
        "free_model_inventory",
        "github_landscape",
        "web_monitoring",
        "source_discovery",
    }
)
_SNAP_INTAKE_FIRST_PARTY = frozenset(
    {"user_directed", "foreground_web", "background_task", "anticipatory_research"}
)


def _classify_source_snapshot(source: str) -> str | None:
    """Frozen env-free source→origin classifier (see the snapshot note above).
    Returns 'external_untrusted' / 'first_party' / None (unknown → fail-closed)."""
    if source in _SNAP_EXTERNAL:
        return "external_untrusted"
    if source in _SNAP_FIRST_PARTY:
        return "first_party"
    if source.startswith("ego_domain_redirect:"):
        return "first_party"  # ego cognition (Genesis COO/CEO); see provenance snapshot
    if source.startswith(_SNAP_INTAKE_PREFIX):
        suffix = source[len(_SNAP_INTAKE_PREFIX) :]
        if suffix in _SNAP_INTAKE_EXTERNAL:
            return "external_untrusted"
        if suffix in _SNAP_INTAKE_FIRST_PARTY:
            return "first_party"
    return None  # module:* / session:* / unknown → fail-closed


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    if not await _has_table(db, "observations"):
        return

    # 0. Grandfather gateway/voice SESSION rows to external_untrusted BEFORE the
    #    session-JOIN below, so their session-attributed observations inherit the
    #    right origin. get_or_create_foreground / register_voice_session now stamp
    #    this at creation, but pre-deploy foreground gateway rows keep NULL forever
    #    (foreground sessions never auto-expire, and reuse never restamps). Key on
    #    the ACTUAL stored channel values ('voice_s2s' for voice, NOT the
    #    ChannelType 'voice'; source_tag='voice' as a belt). NULL-channel rows are
    #    register_from_filesystem-adopted owner CLI sessions — correctly skipped.
    if await _has_table(db, "cc_sessions"):
        await db.execute(
            """
            UPDATE cc_sessions
               SET origin_class = 'external_untrusted'
             WHERE origin_class IS NULL
               AND (channel IN ('web', 'whatsapp') OR channel = 'voice_s2s'
                    OR source_tag = 'voice')
            """
        )

    # 1. Session-attributed rows inherit their session's origin_class.
    #    'session:' is 8 chars → the id starts at position 9 (SQLite substr is
    #    1-indexed). Only set where the joined origin is itself non-NULL.
    if await _has_table(db, "cc_sessions"):
        await db.execute(
            """
            UPDATE observations
               SET origin_class = (
                   SELECT s.origin_class FROM cc_sessions s
                    WHERE s.id = substr(observations.source, 9)
               )
             WHERE origin_class IS NULL
               AND source LIKE 'session:%'
               AND (
                   SELECT s.origin_class FROM cc_sessions s
                    WHERE s.id = substr(observations.source, 9)
               ) IS NOT NULL
            """
        )

    # 2. All other sources: classify via the FROZEN env-free snapshot (above).
    #    Only apply a DEFINITE (non-None) origin; unknown sources stay NULL.
    cursor = await db.execute(
        "SELECT DISTINCT source FROM observations "
        "WHERE origin_class IS NULL AND source IS NOT NULL "
        "AND source NOT LIKE 'session:%'"
    )
    sources = [row[0] for row in await cursor.fetchall()]
    for src in sources:
        origin = _classify_source_snapshot(src)
        if origin is None:
            continue  # fail-closed: leave unknown-source rows NULL
        await db.execute(
            "UPDATE observations SET origin_class = ? WHERE origin_class IS NULL AND source = ?",
            (origin, src),
        )


async def down(db: aiosqlite.Connection) -> None:
    # A backfill is not cleanly reversible — a stamped origin is
    # indistinguishable from one the write path set. No-op.
    return
