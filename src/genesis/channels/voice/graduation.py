"""Graduation-event envelope contract for the voice edge→core boundary (W0).

Shared vocabulary for the ``POST /v1/voice/graduate`` endpoint (and, later,
the W2 policy drainer). Flask-free and stdlib-only on purpose: unit-testable
without an app fixture, importable from both the dashboard route and the
drainer.

Validation is deliberately shallow at this boundary: the quarantine table
lands envelopes verbatim, and per-type payload policy (spec §6.4) belongs to
the drainer. What IS enforced here is the envelope shape itself — the parts
the transport contract (dedup key, type dispatch, provenance class) depends
on. ``schema_version`` is exact-match: a core that doesn't know a version
must reject it (the edge outbox holds the event until the core upgrades),
never quarantine something it can't later interpret.
"""

from __future__ import annotations

from datetime import datetime

SCHEMA_VERSION = 1

EVENT_TYPES = ("perk_up", "memory_candidate", "meeting_summary")

PROVENANCE_CLASSES = ("ambient_overheard", "meeting_capture")

# event_id is an edge-generated UUID; anything longer is malformed input, not
# a UUID variant we should store as a dedup key.
_MAX_EVENT_ID_LEN = 128


def _parseable_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return True


def validate_envelope(data: dict) -> list[str]:
    """Validate a graduation-event envelope. Returns [] if valid.

    Manual dict validation (house pattern — no pydantic in the dashboard
    surfaces). Each failure appends one human-readable string; the route
    returns them all at once so the edge can log a complete rejection reason.
    """
    errors: list[str] = []

    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        errors.append("event_id must be a non-empty string")
    elif len(event_id) > _MAX_EVENT_ID_LEN:
        errors.append(f"event_id exceeds {_MAX_EVENT_ID_LEN} chars")

    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION} (got {data.get('schema_version')!r})"
        )

    if data.get("type") not in EVENT_TYPES:
        errors.append(f"type must be one of {', '.join(EVENT_TYPES)}")

    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        errors.append("source must be a non-empty string")

    occurred_at = data.get("occurred_at")
    if not isinstance(occurred_at, str) or not _parseable_iso(occurred_at):
        errors.append("occurred_at must be an ISO8601 timestamp string")

    if not isinstance(data.get("payload"), dict):
        errors.append("payload must be an object")

    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    elif provenance.get("class") not in PROVENANCE_CLASSES:
        errors.append(f"provenance.class must be one of {', '.join(PROVENANCE_CLASSES)}")

    return errors
