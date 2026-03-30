"""UserModelEvolver — processes user_model_delta observations into the user model."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import observations, user_model
from genesis.memory.types import UserModelSnapshot

# Cluster prefixes — shared with identity.loader for consistent rendering.
_FIELD_CLUSTERS = {
    "risk_tolerance": "Risk Profile",
    "tolerance_for": "Tolerances",
    "preference_for": "Preferences",
    "trust_in": "Trust Dynamics",
    "autonomy": "Autonomy Model",
    "communication": "Communication Style",
    "decision": "Decision Making",
    "system_health": "System Health",
    "operational": "Operational Style",
    "technical": "Technical Approach",
    "recovery_program": "Recovery Approach",
    "prioritization": "Prioritization",
    "risk_appetite": "Risk Appetite",
}

_CLUSTER_VALUE_TRUNCATE = 60


def _canonicalize_field(field_name: str) -> str:
    """Return the cluster prefix key if *field_name* matches, else the original name."""
    for prefix in _FIELD_CLUSTERS:
        if field_name.startswith(prefix):
            return prefix
    return field_name


class UserModelEvolver:
    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self._db = db

    async def process_pending_deltas(
        self,
        *,
        auto_accept_threshold: float = 0.7,
        accumulation_count: int = 3,
    ) -> UserModelSnapshot | None:
        """Process unresolved user_model_delta observations.

        Rules:
        - confidence >= auto_accept_threshold: auto-accept into model
        - confidence < threshold: accumulate; when same field+value appears
          accumulation_count+ times, accept
        - Mark processed deltas as resolved
        """
        pending = await observations.query(
            self._db, type="user_model_delta", resolved=False
        )
        if not pending:
            return None

        # Get current model
        current = await user_model.get_current(self._db)
        model: dict = json.loads(current["model_json"]) if current else {}
        evidence_count = current["evidence_count"] if current else 0

        # Group deltas by (field, value)
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for delta in pending:
            try:
                parsed = json.loads(delta["content"])
                key = (parsed["field"], str(parsed["value"]))
                groups[key].append({**delta, "_parsed": parsed})
            except (json.JSONDecodeError, KeyError):
                continue

        accepted_ids: list[str] = []
        changed = False

        for (field, _value), deltas in groups.items():
            # Check if any delta meets auto-accept threshold
            high_conf = any(
                d["_parsed"].get("confidence", 0) >= auto_accept_threshold
                for d in deltas
            )
            if high_conf or len(deltas) >= accumulation_count:
                # Accept: parse value back from string if needed
                parsed_value = deltas[0]["_parsed"]["value"]
                canonical = _canonicalize_field(field)
                if canonical != field and canonical in model:
                    # Append to existing cluster value
                    existing = str(model[canonical])
                    new_part = str(parsed_value)[:_CLUSTER_VALUE_TRUNCATE]
                    model[canonical] = f"{existing}; {new_part}"
                else:
                    model[canonical] = parsed_value
                accepted_ids.extend(d["id"] for d in deltas)
                evidence_count += len(deltas)
                changed = True

        if not changed:
            return None

        # Mark accepted deltas as resolved
        now = datetime.now(UTC).isoformat()
        for delta_id in accepted_ids:
            await observations.resolve(
                self._db, delta_id, resolved_at=now, resolution_notes="auto-accepted"
            )

        # Upsert updated model
        await user_model.upsert(
            self._db,
            model_json=model,
            synthesized_at=now,
            synthesized_by="user_model_evolver",
            evidence_count=evidence_count,
            last_change_type="delta_processing",
        )

        current = await user_model.get_current(self._db)
        return UserModelSnapshot(
            model=model,
            version=current["version"],
            evidence_count=evidence_count,
            synthesized_at=now,
        )

    async def get_current_model(self) -> UserModelSnapshot | None:
        """Get current user model from cache."""
        row = await user_model.get_current(self._db)
        if not row:
            return None
        return UserModelSnapshot(
            model=json.loads(row["model_json"]),
            version=row["version"],
            evidence_count=row["evidence_count"],
            synthesized_at=row["synthesized_at"],
        )

    async def get_model_summary(self) -> str:
        """Rendered text for context injection."""
        snapshot = await self.get_current_model()
        if snapshot is None:
            return "No user model established yet."
        lines = [
            f"User Model (v{snapshot.version}, {snapshot.evidence_count} evidence points):"
        ]
        for field, value in snapshot.model.items():
            lines.append(f"- {field}: {value}")
        return "\n".join(lines)

    @classmethod
    def consolidate_model(cls, model: dict) -> dict:
        """Merge fields that share a cluster prefix into single entries.

        This is a one-time cleanup: for each prefix in ``_FIELD_CLUSTERS``,
        find all matching fields, concatenate their values (truncated to
        60 chars each), store under the canonical prefix key, and remove
        the original individual fields.

        Returns a new dict; the input is not mutated.
        """
        result = dict(model)
        for prefix in _FIELD_CLUSTERS:
            matching = [
                k for k in list(result) if k.startswith(prefix) and k != prefix
            ]
            if not matching:
                continue
            parts: list[str] = []
            # Preserve any existing canonical value
            if prefix in result:
                parts.append(str(result[prefix])[:_CLUSTER_VALUE_TRUNCATE])
            for key in sorted(matching):
                parts.append(str(result.pop(key))[:_CLUSTER_VALUE_TRUNCATE])
            result[prefix] = "; ".join(parts)
        return result
