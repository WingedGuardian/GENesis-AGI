"""UserModelEvolver — processes user_model_delta observations into the user model."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.crud import observations, user_model
from genesis.memory.types import UserModelSnapshot

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Hard cap on evidence rows pulled into the synthesis prompt — keeps token
# usage bounded so the free-chain providers (mistral-small, groq, gemini)
# don't reject the request. Roughly ~3k input tokens at 20 entries × ~150 chars.
_NARRATIVE_EVIDENCE_LIMIT = 20

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
        accepted_ids_out: list[str] | None = None,
    ) -> UserModelSnapshot | None:
        """Process unresolved user_model_delta observations.

        Rules:
        - confidence >= auto_accept_threshold: auto-accept into model
        - confidence < threshold: accumulate; when same field+value appears
          accumulation_count+ times, accept
        - Mark processed deltas as resolved

        ``accepted_ids_out``: optional caller-supplied list extended with the
        observation ids of the deltas ACCEPTED into the model this run (WS-3
        gate-2 — lets the USER_KNOWLEDGE writer aggregate their stored
        origin_class without changing this method's return contract).
        """
        from genesis.security.immunity import TRUSTED_PRIVILEGED_WRITE_ORIGINS

        # WS-3 poisoning gate: fetch ONLY trusted-origin (owner/first_party)
        # unresolved deltas, filtered in SQL. Filtering in the query (not in
        # Python after a LIMITed fetch) is load-bearing: it stops a flood of
        # forged/NULL-origin deltas from crowding trusted deltas out of the
        # newest-N window (a persistent user-model-learning DoS). Fail-closed —
        # NULL origin is excluded by IN-semantics. Legit reflection deltas carry
        # first_party in quiet windows (reflection_window_origin never returns
        # None), so the normal pipeline is unaffected. Barred rows are left
        # unresolved for the 14-day TTL to reap and are ALSO excluded from
        # synthesize_narrative's evidence query below, so they can never launder
        # into USER_KNOWLEDGE.md after TTL-resolution. This is an unconditional
        # invariant (like never-block-owner), NOT wired through the
        # shadow-clamped gate_mode("identity") — that would be a silent no-op.
        pending = await observations.query(
            self._db,
            type="user_model_delta",
            resolved=False,
            origin_class_in=list(TRUSTED_PRIVILEGED_WRITE_ORIGINS),
        )
        # Observability: surface a poisoning-attempt signal (COUNT only — never
        # re-fetch untrusted content into the accept path). process_pending_deltas
        # runs on a multi-hour cadence, so a warning here is a signal, not spam.
        try:
            _ph = ",".join("?" for _ in TRUSTED_PRIVILEGED_WRITE_ORIGINS)
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM observations "  # noqa: S608 - _ph is only "?" placeholders; values bound as params
                "WHERE type = 'user_model_delta' AND resolved = 0 "
                f"AND (origin_class IS NULL OR origin_class NOT IN ({_ph}))",
                TRUSTED_PRIVILEGED_WRITE_ORIGINS,
            )
            row = await cur.fetchone()
            if row and row[0]:
                logger.warning(
                    "%d untrusted user_model_delta(s) present, BARRED from "
                    "auto-accept (origin not owner/first_party) — left for TTL",
                    row[0],
                )
        except Exception:
            logger.debug("barred user_model_delta count failed", exc_info=True)
        if not pending:
            # Touch synthesized_at to confirm model is current even when there
            # is no TRUSTED pending work — an empty query OR every pending delta
            # quarantined above. Without this, the user_goal_staleness signal's
            # project-staleness path decays to 1.0 whenever the delta pipeline is
            # idle (no new observations to process).
            try:
                await self._db.execute(
                    "UPDATE user_model_cache SET synthesized_at = ? "
                    "WHERE id = 'current'",
                    (datetime.now(UTC).isoformat(),),
                )
                await self._db.commit()
            except Exception:
                logger.debug("Failed to touch synthesized_at", exc_info=True)
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

        if accepted_ids_out is not None:
            accepted_ids_out.extend(accepted_ids)

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

    async def synthesize_narrative(
        self,
        router: Router,
        *,
        evidence_count: int = 0,
        call_site_id: str = "11_user_model_synthesis",
    ) -> str | None:
        """Use an LLM to synthesize a narrative summary of the user model.

        Reads the current model dict from the cache, pulls a bounded sample
        of recent ``user_model_delta`` observations as supporting evidence,
        and asks the router for a structured narrative. Returns the narrative
        text on success, or None when the call fails (all free providers
        exhausted, malformed response, etc).

        Callers must treat None as graceful degradation — fall back to the
        rules-based dict rendering instead of raising.
        """
        snapshot = await self.get_current_model()
        if snapshot is None or not snapshot.model:
            return None

        # Pull recent evidence (most recent resolved deltas) for context.
        # WS-3: restrict to trusted-origin deltas — a barred external/NULL delta
        # becomes resolved=True once its 14-day TTL fires (resolve_expired), so
        # WITHOUT this filter it would launder into the narrative that overwrites
        # USER_KNOWLEDGE.md. Same trusted set as the accept path above.
        from genesis.security.immunity import TRUSTED_PRIVILEGED_WRITE_ORIGINS

        try:
            recent = await observations.query(
                self._db,
                type="user_model_delta",
                resolved=True,
                origin_class_in=list(TRUSTED_PRIVILEGED_WRITE_ORIGINS),
                limit=_NARRATIVE_EVIDENCE_LIMIT,
            )
        except Exception:
            logger.debug("Failed to fetch evidence for synthesis", exc_info=True)
            recent = []

        prompt = self._build_synthesis_prompt(
            snapshot.model, recent, evidence_count=evidence_count,
        )

        try:
            result = await router.route_call(
                call_site_id,
                [{"role": "user", "content": prompt}],
            )
        except Exception:
            logger.warning(
                "User-model synthesis call raised — falling back to "
                "rules-based rendering",
                exc_info=True,
            )
            return None

        if not result or not result.success or not result.content:
            logger.info(
                "User-model synthesis failed (provider=%s, error=%s) — "
                "falling back to rules-based rendering",
                getattr(result, "provider_used", None),
                getattr(result, "error", None),
            )
            return None

        narrative = result.content.strip()
        if not narrative:
            return None

        logger.info(
            "User-model synthesis OK (provider=%s, in=%d, out=%d, cost=$%.4f)",
            result.provider_used,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
        )
        return narrative

    @staticmethod
    def _build_synthesis_prompt(
        model: dict,
        recent_deltas: list[dict],
        *,
        evidence_count: int,
    ) -> str:
        """Construct the LLM prompt for narrative synthesis.

        Kept simple and bounded: model dict (truncated values) + a short list
        of recent evidence snippets. The prompt asks for a structured but
        narrative summary, in first-person Genesis voice, suitable for
        injection into USER_KNOWLEDGE.md.
        """
        # Render model dict as a compact key/value list, truncating long values
        model_lines = []
        for field in sorted(model):
            value_str = str(model[field])
            if len(value_str) > 240:
                value_str = value_str[:237] + "..."
            display = field.replace("_", " ")
            model_lines.append(f"- {display}: {value_str}")
        model_block = "\n".join(model_lines) if model_lines else "(empty)"

        # Render recent evidence: parse the JSON content of each delta
        evidence_lines = []
        for delta in recent_deltas[:_NARRATIVE_EVIDENCE_LIMIT]:
            try:
                parsed = json.loads(delta.get("content", "{}"))
            except (TypeError, ValueError):
                continue
            field = parsed.get("field", "?")
            value = parsed.get("value", "?")
            evidence = parsed.get("evidence", "")
            value_str = str(value)
            if len(value_str) > 80:
                value_str = value_str[:77] + "..."
            evidence_str = str(evidence)
            if len(evidence_str) > 160:
                evidence_str = evidence_str[:157] + "..."
            evidence_lines.append(
                f"- {field} = {value_str}  — {evidence_str}"
            )
        evidence_block = (
            "\n".join(evidence_lines) if evidence_lines else "(no recent deltas)"
        )

        return (
            "You are Genesis, synthesizing what you know about your user into a "
            "structured knowledge file. Write in first person from Genesis's "
            "perspective. Be concrete and specific. Avoid generic AI prose. "
            "Group related fields into themes; do not just list raw key/value "
            "pairs. Aim for 6-12 short paragraphs total, organized under "
            "level-2 markdown headings (## Theme).\n"
            "\n"
            "Include a '## Life Structure' section that maps the user's life "
            "across these dimensions (skip any that have no evidence):\n"
            "- How they earn (employment, freelance, etc.)\n"
            "- What they're building (projects, products, portfolio)\n"
            "- Who they connect with (professional/personal networks)\n"
            "- What they maintain (health, finances, obligations)\n"
            "- What they pursue (learning, hobbies, passions)\n"
            "\n"
            f"Current user model ({evidence_count} evidence points accumulated):\n"
            f"{model_block}\n"
            "\n"
            "Recent supporting evidence (most recent first):\n"
            f"{evidence_block}\n"
            "\n"
            "Output ONLY the markdown body — no preamble, no closing remarks. "
            "Use H2 (##) section headings. Do not include a top-level H1 title; "
            "the file already has one. Do not invent facts not supported by the "
            "model or evidence above."
        )

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
