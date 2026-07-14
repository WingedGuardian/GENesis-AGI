"""Profile drift detection — a section-hash change IS a signal.

Compares the freshly collected profile against the previous one and emits a
durable observation per changed section via ``observations.create`` with
``skip_if_duplicate=True`` (dedup per (source, content_hash) against
unresolved rows — verified semantics, ``db/crud/observations.py``).

Rules:
- First-ever run (no previous profile) emits nothing.
- A section newly APPEARING or newly becoming unavailable is NOT drift
  (plane rollout / degradation, not a config change).
- Sections whose collection FAILED keep their prior hash upstream (service
  merge), so they can never produce phantom drift here.
- DB failure degrades to an event-bus warning; drift detection must never
  break a refresh.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Sections whose drift is high-priority: the incident classes that hurt most.
_HIGH_PRIORITY_SECTIONS = {"memory", "storage", "kernel", "sqlite"}

_MAX_CHANGED_PATHS = 10


def _changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Recursive key-path diff of two fact trees (capped by the caller)."""
    if type(old) is not type(new):
        return [prefix or "<root>"]
    if isinstance(old, dict):
        paths: list[str] = []
        for key in sorted(set(old) | set(new)):
            sub_prefix = f"{prefix}.{key}" if prefix else str(key)
            if key not in old:
                paths.append(f"{sub_prefix} (added)")
            elif key not in new:
                paths.append(f"{sub_prefix} (removed)")
            elif old[key] != new[key]:
                paths.extend(_changed_paths(old[key], new[key], sub_prefix))
        return paths
    if old != new:
        return [prefix or "<root>"]
    return []


def compute_drift(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return drift records for sections whose fact hash changed.

    Each record: {section, old_hash, new_hash, changed_paths, priority}.
    """
    prev_sections = previous.get("sections", {})
    curr_sections = current.get("sections", {})
    if not prev_sections:
        return []  # first run — nothing to compare against

    drift: list[dict[str, Any]] = []
    for name, curr in curr_sections.items():
        prev = prev_sections.get(name)
        if prev is None:
            continue  # newly appeared ≠ drift
        # No status gate: the hash guards below already absorb availability
        # flips (a never-ok section has no hash; an unavailable/error section
        # keeps its prior facts+hash through the outage), while a fact that
        # REALLY changed during an outage window still surfaces on recovery —
        # a status gate silently swallowed exactly that case (review
        # 2026-07-13).
        old_hash, new_hash = prev.get("hash"), curr.get("hash")
        if not old_hash or not new_hash or old_hash == new_hash:
            continue
        paths = _changed_paths(prev.get("facts", {}), curr.get("facts", {}))
        drift.append(
            {
                "section": name,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "changed_paths": paths[:_MAX_CHANGED_PATHS],
                "truncated": len(paths) > _MAX_CHANGED_PATHS,
                "priority": "high" if name in _HIGH_PRIORITY_SECTIONS else "medium",
            },
        )
    return drift


async def emit_drift_observations(db, drift: list[dict[str, Any]], event_bus=None) -> int:
    """Persist one dedup-gated observation per drifted section. Returns count written."""
    if db is None or not drift:
        return 0

    from genesis.db.crud import observations

    written = 0
    for record in drift:
        section = record["section"]
        paths = ", ".join(record["changed_paths"]) or "<unresolved>"
        suffix = ", …" if record.get("truncated") else ""
        content = f"{section}: infrastructure facts changed — {paths}{suffix}"
        # Dedup key = DESTINATION state only. Keying on (old→new) lets a fact
        # flapping between two values mint a new hash every transition
        # (A→B, B→A, A→B …) and flood unresolved observations; keyed on the
        # destination, an oscillation collapses to at most one open
        # observation per endpoint (review 2026-07-12).
        content_hash = hashlib.sha256(
            f"{section}:{record['new_hash']}".encode(),
        ).hexdigest()
        try:
            result = await observations.create(
                db,
                id=f"infra-drift-{uuid.uuid4().hex[:8]}",
                source="infra_profile",
                type="infrastructure_drift",
                content=content,
                priority=record["priority"],
                created_at=datetime.now(UTC).isoformat(),
                content_hash=content_hash,
                skip_if_duplicate=True,
            )
            if result is not None:
                written += 1
        except Exception:
            logger.warning(
                "infra_profile: drift observation write failed for %s",
                section,
                exc_info=True,
            )
            if event_bus is not None:
                try:
                    from genesis.observability.events import Severity, Subsystem

                    # emit is a coroutine — unawaited it never reaches the
                    # ring/persist queue (Codex P2, 2026-07-12).
                    await event_bus.emit(
                        Subsystem.OBSERVABILITY,
                        Severity.WARNING,
                        "infra_profile_drift_write_failed",
                        f"drift observation write failed for section {section}",
                    )
                except Exception:
                    logger.debug("infra_profile: event-bus fallback also failed")
    return written
