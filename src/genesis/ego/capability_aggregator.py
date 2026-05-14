"""Capability aggregator — builds the ego's self-model from multiple data sources.

Queries intervention journal, ego proposals, autonomy state, procedural
memory, and CC sessions to compute per-domain confidence scores.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def compute_capability_map(db: aiosqlite.Connection) -> list[dict]:
    """Aggregate data from 5 sources into domain-level capability scores.

    Returns a list of dicts: {domain, confidence, sample_size, trend, evidence}.
    Each source is queried independently — failures are logged and skipped.
    """
    domains: dict[str, _DomainAccumulator] = {}

    # 1. Intervention journal — proposal outcome rates by action_type
    # Excludes withdrawn/tabled from denominator — these are lifecycle
    # events, not user decisions on proposal quality.
    try:
        from genesis.db.crud import intervention_journal as journal_crud
        aggs = await journal_crud.aggregate_by_type(db)
        for row in aggs:
            domain = row["action_type"]
            # Only count terminal user-decision states in denominator
            success = row.get("approved", 0) + row.get("executed", 0)
            rejected = row.get("rejected", 0) + row.get("failed", 0)
            total = success + rejected
            if total == 0:
                continue
            rate = success / total
            acc = domains.setdefault(domain, _DomainAccumulator(domain))
            acc.add_signal("journal", rate, total)
    except Exception:
        logger.debug("Capability aggregation: intervention_journal unavailable")

    # 2. Ego proposals — approval rates by action_type (30d)
    # Excludes withdrawn/tabled/expired from denominator — only count
    # proposals that reached a terminal user-decision state.
    try:
        cur = await db.execute(
            """SELECT action_type,
                      SUM(CASE WHEN status IN ('approved', 'executed') THEN 1 ELSE 0 END) as success,
                      SUM(CASE WHEN status IN ('rejected', 'failed') THEN 1 ELSE 0 END) as rejected
               FROM ego_proposals
               WHERE created_at >= datetime('now', '-30 days')
                 AND status IN ('approved', 'executed', 'rejected', 'failed')
               GROUP BY action_type"""
        )
        for action_type, success, rejected in await cur.fetchall():
            total = success + rejected
            if total == 0:
                continue
            rate = success / total
            acc = domains.setdefault(action_type, _DomainAccumulator(action_type))
            acc.add_signal("proposals", rate, total)
    except Exception:
        logger.debug("Capability aggregation: ego_proposals unavailable")

    # 3. Autonomy state — Bayesian posteriors by category
    try:
        cur = await db.execute(
            """SELECT category,
                      ROUND(CAST(total_successes + 1 AS REAL) / (total_successes + total_corrections + 2), 3) as posterior,
                      total_successes + total_corrections as total
               FROM autonomy_state
               WHERE total_successes + total_corrections > 0"""
        )
        for category, posterior, total in await cur.fetchall():
            acc = domains.setdefault(category, _DomainAccumulator(category))
            acc.add_signal("autonomy", posterior, total)
    except Exception:
        logger.debug("Capability aggregation: autonomy_state unavailable")

    # 4. Procedural memory — avg confidence per task_type
    try:
        cur = await db.execute(
            """SELECT task_type,
                      ROUND(AVG(confidence), 3) as avg_conf,
                      COUNT(*) as total
               FROM procedural_memory
               WHERE deprecated = 0 AND quarantined = 0
               GROUP BY task_type"""
        )
        for task_type, avg_conf, total in await cur.fetchall():
            acc = domains.setdefault(task_type, _DomainAccumulator(task_type))
            acc.add_signal("procedures", avg_conf, total)
    except Exception:
        logger.debug("Capability aggregation: procedural_memory unavailable")

    # 5. CC sessions — completion rate by source_tag (30d)
    try:
        cur = await db.execute(
            """SELECT source_tag,
                      COUNT(*) as total,
                      SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed
               FROM cc_sessions
               WHERE started_at >= datetime('now', '-30 days')
                 AND source_tag != 'foreground'
               GROUP BY source_tag
               HAVING total >= 3"""
        )
        for source_tag, total, completed in await cur.fetchall():
            rate = completed / total if total > 0 else 0.0
            acc = domains.setdefault(source_tag, _DomainAccumulator(source_tag))
            acc.add_signal("sessions", rate, total)
    except Exception:
        logger.debug("Capability aggregation: cc_sessions unavailable")

    # Compute composite scores
    results = []
    for acc in domains.values():
        score = acc.composite()
        if score is not None:
            results.append(score)

    # Sort by confidence descending
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


async def refresh_capability_map(db: aiosqlite.Connection) -> int:
    """Recompute capability map and persist to database.

    Returns the number of domains updated.
    """
    from genesis.db.crud import capability_map as cap_crud

    results = await compute_capability_map(db)
    for entry in results:
        await cap_crud.upsert(
            db,
            domain=entry["domain"],
            confidence=entry["confidence"],
            sample_size=entry["sample_size"],
            trend=entry.get("trend", "stable"),
            evidence_summary=entry.get("evidence", ""),
        )
    logger.info("Capability map refreshed: %d domains", len(results))
    return len(results)


class _DomainAccumulator:
    """Accumulates signals from multiple sources for a single domain."""

    def __init__(self, domain: str):
        self.domain = domain
        self.signals: list[tuple[str, float, int]] = []  # (source, rate, sample_size)

    def add_signal(self, source: str, rate: float, sample_size: int) -> None:
        self.signals.append((source, rate, sample_size))

    def composite(self) -> dict | None:
        if not self.signals:
            return None

        # Weighted average: weight by sample size (more data = more influence)
        total_weight = sum(n for _, _, n in self.signals)
        if total_weight == 0:
            return None

        weighted_sum = sum(rate * n for _, rate, n in self.signals)
        confidence = round(weighted_sum / total_weight, 3)

        # Build evidence summary
        parts = []
        for source, rate, n in self.signals:
            parts.append(f"{source}:{rate:.0%}({n})")
        evidence = ", ".join(parts)

        return {
            "domain": self.domain,
            "confidence": confidence,
            "sample_size": total_weight,
            "trend": "stable",  # trend requires historical data; start with stable
            "evidence": evidence,
        }
