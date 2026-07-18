"""Phantom-signal claim guard for reflection narrative text.

Deep/light reflections write free-text narrative into ``cognitive_state``
(``active_context``, focus directives). When that narrative asserts a
signal *by name and value* that was NOT in the live tick, the claim
persists for days and re-enters every later reflection's context — the
next cycle then "debunks" it, the one after re-asserts it (the phantom
assert/debunk loop; follow-up d8603c0b).

This guard validates ``name=value`` / ``name: value`` claims in narrative
text against the actual tick signal set. A claim is a violation iff the
name is a REGISTERED signal (``signal_weights``) that was absent from the
tick — unknown names are left alone (prose, not signal claims). Violations
are annotated-and-stripped, logged, and recorded as a
``phantom_signal_claim`` observation. The guard NEVER rejects or blocks a
cognitive-state update, and any internal error passes the text through
unchanged — losing a narrative update over a guard bug would be worse
than the noise it prevents.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# snake_case identifier followed by = or : and a bare number. Requires ≥2
# underscores-or-chars beyond a single word to look like a signal name is
# too strict; instead we rely on registry membership for precision.
_CLAIM_RE = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*[=:]\s*(-?[0-9]+(?:\.[0-9]+)?)"
)


async def registry_signal_names(db) -> set[str]:
    """Names of all known signals: ``signal_weights`` UNION names observed
    in recent ``awareness_ticks``.

    The union matters: several live collectors are not seeded into
    signal_weights (they vary by install), so the weights table alone
    leaves their claims unguardable. Recent ticks are ground truth for
    what this install actually collects — install-agnostic, no seed-list
    maintenance. Empty set on total failure — which makes the guard a
    no-op, never a blocker."""
    names: set[str] = set()
    try:
        cursor = await db.execute("SELECT signal_name FROM signal_weights")
        names.update(row[0] for row in await cursor.fetchall())
    except Exception:
        logger.debug("signal_weights registry query failed", exc_info=True)
    try:
        cursor = await db.execute(
            "SELECT signals_json FROM awareness_ticks "
            "ORDER BY created_at DESC LIMIT 12"
        )
        for (signals_json,) in await cursor.fetchall():
            try:
                entries = json.loads(signals_json) if signals_json else []
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(entries, dict):
                entries = list(entries.values())
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name"):
                    names.add(str(entry["name"]))
    except Exception:
        logger.debug("awareness_ticks registry query failed", exc_info=True)
    return names


def validate_signal_claims(
    text: str,
    *,
    tick_signal_names: set[str] | None,
    registry_names: set[str],
) -> tuple[str, list[str]]:
    """Return ``(cleaned_text, violated_names)``.

    A claim is stripped iff its name is in ``registry_names`` but not in
    ``tick_signal_names``. ``tick_signal_names=None`` disables validation
    (back-compat for callers that cannot provide the tick). Never raises.
    """
    if not text or tick_signal_names is None or not registry_names:
        return text, []
    try:
        violations: list[str] = []

        def _repl(match: re.Match) -> str:
            name = match.group(1)
            if name in registry_names and name not in tick_signal_names:
                violations.append(name)
                return f"[unverified signal claim removed: {name}]"
            return match.group(0)

        return _CLAIM_RE.sub(_repl, text), violations
    except Exception:
        logger.warning("signal-claim guard error — passing text through", exc_info=True)
        return text, []


async def guard_narrative(
    db,
    text: str | None,
    *,
    tick_signal_names: set[str] | None,
    source: str,
) -> str | None:
    """Validate *text* against the tick; strip violations, log, and record a
    ``phantom_signal_claim`` observation. Returns the (possibly annotated)
    text — never ``None``-out, never raises, never blocks the update."""
    if not text or tick_signal_names is None:
        return text
    try:
        registry = await registry_signal_names(db)
        cleaned, violations = validate_signal_claims(
            text, tick_signal_names=tick_signal_names, registry_names=registry,
        )
        if violations:
            logger.warning(
                "Stripped %d unverified signal claim(s) from %s narrative: %s",
                len(violations), source, violations,
            )
            try:
                from genesis.db.crud import observations

                await observations.create(
                    db,
                    id=str(uuid.uuid4()),
                    source=source,
                    type="phantom_signal_claim",
                    content=json.dumps({
                        "stripped_signals": violations,
                        "note": (
                            "Narrative asserted registered signals by "
                            "name/value that were absent from the live tick."
                        ),
                    }),
                    priority="low",
                    created_at=datetime.now(UTC).isoformat(),
                )
            except Exception:
                logger.debug("phantom_signal_claim observation write failed", exc_info=True)
        return cleaned
    except Exception:
        logger.warning("signal-claim guard failed — passing text through", exc_info=True)
        return text
