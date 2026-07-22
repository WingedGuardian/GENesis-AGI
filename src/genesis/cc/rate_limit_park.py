"""Rate-limit park orchestration — the thin layer the CC call sites delegate to.

Keeps `conversation.py` / `direct_session.py` (both already over the file-size
cap) free of park logic. Responsibilities:

- ``park_conversation`` — a foreground turn exhausted failover+contingency: park
  the user's prompt, write the display resume time, return mode-aware copy.
- ``park_direct_session`` — a background session hit the limit: park the full
  request (fresh), OR — if this session was itself a resume (caller_context
  carries a park id) — re-limit the SAME park in place (attempts+1, backoff),
  preserving the lineage the escalation counter depends on.
- ``mark_resumed_if_lineage`` — a resumed session delivered successfully: close
  its park.

All writes are best-effort and mode-gated (``off`` → no park, current behavior).
``now`` is injectable for deterministic tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.cc import rate_limit_resume_config as cfg_mod
from genesis.cc.rate_limit_reset import parse_reset
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.db.crud import cc_sessions

logger = logging.getLogger("genesis.cc.rate_limit_park")

_RESUME_PREFIX = "rate_limit_resume:"


@dataclass
class ParkOutcome:
    """Result of a foreground park — what the handler returns to the user."""

    parked: bool
    park_id: str | None
    reset_at: datetime | None
    mode: str
    copy: str


# ── helpers ──────────────────────────────────────────────────────────────
def dedup_key(kind: str, origin_session_id: str | None, prompt: str) -> str:
    """Stable per-logical-work key: same (kind, origin, prompt) → same open park."""
    raw = f"{kind}\x00{origin_session_id or ''}\x00{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_park_id(caller_context: str | None) -> str | None:
    """Extract a park id from a ``rate_limit_resume:<id>`` caller_context."""
    if caller_context and caller_context.startswith(_RESUME_PREFIX):
        return caller_context[len(_RESUME_PREFIX) :] or None
    return None


def _enum_str(value: object, default: str) -> str:
    """Normalise a CCModel/EffortLevel enum-or-string to its wire string."""
    if value is None:
        return default
    return str(getattr(value, "value", value))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def next_attempt_at(reset_at: datetime | None, now: datetime, cfg: dict) -> str:
    """First re-attempt time: the reset (if known) else a cadence floor, both
    with jitter. Jitter is deterministic (derived from now) so tests are stable."""
    jitter = timedelta(seconds=cfg_mod.knob_int(cfg, "jitter_seconds") * (now.second % 2 + 1) / 2)
    if reset_at is not None:
        base = reset_at if reset_at > now else now
    else:
        base = now + timedelta(minutes=cfg_mod.knob_int(cfg, "cadence_floor_minutes"))
    return _iso(base + jitter)


def backoff_next_attempt(attempts: int, now: datetime, cfg: dict) -> str:
    """Exponential backoff for a re-limited retry: base·2^attempts capped."""
    base = cfg_mod.knob_int(cfg, "backoff_base_minutes")
    cap = cfg_mod.knob_int(cfg, "backoff_cap_minutes")
    minutes = min(base * (2 ** max(0, attempts)), cap)
    return _iso(now + timedelta(minutes=minutes))


def _fmt_reset(reset_at: datetime | None) -> str:
    if reset_at is None:
        return ""
    try:
        return f" around {reset_at.strftime('%-I:%M %p %Z').strip()}"
    except ValueError:
        return ""


def resume_copy(mode: str, reset_at: datetime | None) -> str:
    """Mode-aware foreground message. Replaces the old sentence that promised
    automatic resume unconditionally (nothing backed it)."""
    if mode == "live":
        return (
            "[Rate limit reached — your request is parked and will auto-resume "
            f"and deliver here{_fmt_reset(reset_at)}.]"
        )
    if mode == "propose_only":
        return (
            "[Rate limit reached — your request is parked. Reply 'resume' when "
            "capacity returns and I'll pick it back up.]"
        )
    # off
    return (
        "[Rate limit reached — Genesis is temporarily running in reduced mode. "
        "Please try again later.]"
    )


# ── foreground ───────────────────────────────────────────────────────────
async def park_conversation(
    db: aiosqlite.Connection,
    *,
    prompt: str,
    origin_session_id: str,
    exc: Exception,
    model: object = None,
    effort: object = None,
    timeout_s: int = 3600,
    now: datetime | None = None,
    mode: str | None = None,
) -> ParkOutcome:
    """Park an exhausted foreground turn and return the user-facing copy."""
    now = now or datetime.now(UTC)
    mode = mode if mode is not None else cfg_mod.effective_mode()
    cfg = cfg_mod.load_config()

    raw_event = getattr(exc, "raw_event", None)
    raw_text = getattr(exc, "raw_text", None)
    limit_kind, reset_at = parse_reset(raw_event=raw_event, raw_text=raw_text, now=now)

    if mode == "off":
        return ParkOutcome(False, None, reset_at, mode, resume_copy(mode, reset_at))

    payload = {
        "prompt": prompt,
        "profile": cfg_mod.resume_profile(cfg),
        "model": _enum_str(model, "sonnet"),
        "effort": _enum_str(effort, "high"),
        "timeout_s": int(timeout_s),
        "roster_model": None,
    }
    park_id: str | None = None
    try:
        park_id = await parks.upsert_open_park(
            db,
            kind="conversation",
            dedup_key=dedup_key("conversation", origin_session_id, prompt),
            payload=payload,
            origin_session_id=origin_session_id,
            limit_kind=limit_kind,
            raw_signal=(raw_text or (json.dumps(raw_event) if raw_event else None)),
            reset_at=_iso(reset_at) if reset_at else None,
            next_attempt_at=next_attempt_at(reset_at, now, cfg),
        )
        # Single source of truth: the display resumes_at mirrors the park's reset.
        await cc_sessions.update_rate_limit(
            db,
            origin_session_id,
            rate_limited_at=_iso(now),
            rate_limit_resumes_at=_iso(reset_at) if reset_at else None,
        )
    except Exception:
        logger.error("Failed to park conversation turn %s", origin_session_id[:8], exc_info=True)
        return ParkOutcome(False, None, reset_at, mode, resume_copy(mode, reset_at))

    return ParkOutcome(True, park_id, reset_at, mode, resume_copy(mode, reset_at))


# ── background ───────────────────────────────────────────────────────────
async def park_direct_session(
    db: aiosqlite.Connection,
    *,
    request: object,
    exc: Exception,
    now: datetime | None = None,
    mode: str | None = None,
) -> str | None:
    """Park (or re-limit) a background session that hit a rate limit.

    Returns the park id if the work was parked/re-parked, or None when parking
    is disabled (mode=off) or failed — the caller then keeps current behavior.
    """
    now = now or datetime.now(UTC)
    mode = mode if mode is not None else cfg_mod.effective_mode()
    if mode == "off":
        return None
    cfg = cfg_mod.load_config()

    raw_event = getattr(exc, "raw_event", None)
    raw_text = getattr(exc, "raw_text", None)
    limit_kind, reset_at = parse_reset(raw_event=raw_event, raw_text=raw_text, now=now)

    caller_context = getattr(request, "caller_context", None)
    lineage_id = parse_park_id(caller_context)

    try:
        # A resumed session re-limited → update its own park in place so the
        # attempts counter (and the needs_user escalation) survives (architect #1).
        if lineage_id is not None:
            park = await parks.get_by_id(db, lineage_id)
            attempts = (park["attempts"] if park else 0) + 1
            reset_iso = _iso(reset_at) if reset_at else None
            na = (
                _iso(reset_at)
                if (reset_at and reset_at > now)
                else backoff_next_attempt(attempts, now, cfg)
            )
            status = await parks.relimit(
                db,
                lineage_id,
                reset_at=reset_iso,
                next_attempt_at=na,
                needs_user_at_attempts=cfg_mod.knob_int(cfg, "needs_user_attempts"),
            )
            if status:
                return lineage_id
            # Lineage row gone/terminal (pruned, or a race with
            # recover_stale_resuming) — fall through to a FRESH park so the work
            # is never dropped, rather than returning None → generic failure.
            logger.warning(
                "Resume park %s not resolvable (relimit no-op) — re-parking fresh",
                lineage_id,
            )

        # Fresh background park — serialize enough to re-dispatch verbatim.
        prompt = getattr(request, "prompt", "")
        origin = getattr(request, "origin_session_id", None)
        payload = {
            "prompt": prompt,
            "profile": getattr(request, "profile", "observe"),
            "model": _enum_str(getattr(request, "model", None), "sonnet"),
            "effort": _enum_str(getattr(request, "effort", None), "high"),
            "timeout_s": int(getattr(request, "timeout_s", 3600)),
            "roster_model": getattr(request, "roster_model", None),
        }
        return await parks.upsert_open_park(
            db,
            kind="direct_session",
            dedup_key=dedup_key("direct_session", origin, prompt),
            payload=payload,
            origin_session_id=origin,
            limit_kind=limit_kind,
            raw_signal=(raw_text or (json.dumps(raw_event) if raw_event else None)),
            reset_at=_iso(reset_at) if reset_at else None,
            next_attempt_at=next_attempt_at(reset_at, now, cfg),
        )
    except Exception:
        logger.error("Failed to park direct session", exc_info=True)
        return None


async def mark_resumed_if_lineage(db: aiosqlite.Connection, caller_context: str | None) -> None:
    """Close the park of a resumed session that just delivered successfully."""
    park_id = parse_park_id(caller_context)
    if park_id is None:
        return
    try:
        await parks.mark_resumed(db, park_id)
    except Exception:
        logger.error("Failed to mark park %s resumed", park_id, exc_info=True)
