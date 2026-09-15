"""Engagement tracking — timeout detection and reply recording."""

from __future__ import annotations

import html
import logging
import unicodedata
from email.utils import parseaddr

import aiosqlite

from genesis.db.crud import outreach as outreach_crud

logger = logging.getLogger(__name__)


def _sanitize_ping_field(value: str) -> str:
    """Make an attacker-controlled email field safe for the owner's Telegram ping.

    The outreach delivery path sends with ``parse_mode="HTML"`` and does NOT
    escape (verified 2026-09-01: ``pipeline._deliver`` → ``adapter.send_message``
    with no parse_mode → ``adapter_v2`` defaults to HTML → ``safe_send_message``
    sends the text raw — ``md_to_telegram_html``/``html.escape`` runs only on the
    interactive chat-reply path, never here). So a reply's ``sender``/``subject``/
    ``body`` would otherwise render as live HTML (clickable links, formatting) in
    the owner's trusted chat. Strip Unicode control/format chars (categories
    ``Cc``/``Cf`` — bidi overrides, zero-width) so a display string can't be
    visually disguised, then HTML-escape. Truncate BEFORE calling this so a cut
    never lands mid-entity.
    """
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf"))
    return html.escape(cleaned)


def make_reply_engagement_bridge(tracker: EngagementTracker, notify_owner=None):
    """Build the mail→outreach reply bridge for ``ReplyPoller.set_engagement_bridge``.

    The poller calls it with the matched ``thread`` dict (whose ``context`` was
    stashed at send time by ``OutreachPipeline._deliver`` thread registration)
    and the parsed reply. Resolves ``context["outreach_id"]`` and records the
    engagement NULL-guarded. Every miss degrades to a logged no-op — reply
    tracking must never depend on this bridge. Injected from
    ``runtime/init/outreach.py`` so ``genesis.mail`` stays outreach-agnostic.

    ``notify_owner`` (optional): an async ``(thread, reply) -> None`` callable
    fired AFTER the genuine-reply gates, but ONLY when the thread recipient is a
    curated marketing prospect (``marketing_prospects.get_by_email``), so the
    owner gets a Telegram ping when a real human replies to a cold pitch.
    Recipient-membership (not the thread's ``signal_type``, which the
    owner-approval resume path overwrites) is what makes this fire on the default
    held→approved send path. Best-effort and self-contained: a ping failure is
    logged and swallowed here, so it never breaks reply tracking and never counts
    as a bridge error.
    """

    async def _bridge(thread: dict, reply) -> None:
        context = thread.get("context")
        if not isinstance(context, dict):
            logger.debug(
                "Reply thread %s has no context dict — engagement bridge skipped",
                thread.get("id"),
            )
            return
        outreach_id = context.get("outreach_id")
        if not outreach_id:
            # Pre-bridge sends (e.g. historical foreground sends) have no
            # outreach_id — genuine engagement, but no ledger row to update.
            logger.debug(
                "Reply thread %s context lacks outreach_id — engagement bridge skipped",
                thread.get("id"),
            )
            return
        # Only a genuine, human reply from the address we actually mailed counts
        # as engagement. match_reply accepts any message carrying our Message-ID,
        # so gate out automated mail (OOO/bounce/list) and foreign/spoofed senders
        # before they inflate the reply rate / resolve ledger predictions.
        if getattr(reply, "is_auto", False):
            logger.debug(
                "Reply on thread %s is automated (OOO/bounce/list) — not counted as engagement",
                thread.get("id"),
            )
            return
        recipient = (thread.get("recipient") or "").strip().lower()
        sender_addr = parseaddr(getattr(reply, "sender", "") or "")[1].strip().lower()
        if recipient and sender_addr and sender_addr != recipient:
            logger.debug(
                "Reply on thread %s from %s != recipient %s — not counted as engagement",
                thread.get("id"), sender_addr, recipient,
            )
            return
        reply_text = getattr(reply, "body_preview", "") or ""
        await tracker.record_reply_if_pending(str(outreach_id), reply_text)

        # Owner ping on a genuine reply to a cold marketing pitch (notify-only).
        # The reply has already cleared the auto-responder + foreign-sender gates
        # above, so it's a real human reply. Marketing-ness is keyed on the
        # RECIPIENT being a curated marketing prospect — NOT the thread's
        # signal_type: the owner-approval resume path (pipeline.deliver_approved)
        # rebuilds the send with signal_type="email_gate_resume", so
        # "marketing_cold" never survives a held→approved cold send (the DEFAULT
        # gated path). Every marketing_send resolves its address in code from
        # marketing_prospects, so recipient-membership fires for exactly the
        # marketing replies and is send-path-independent. Membership (not
        # is_active_recipient) is intentional: a contacted / opted-out prospect
        # who replies is still a real human reply worth surfacing. Best-effort:
        # any failure is logged and swallowed so reply tracking never depends on
        # it (and it never counts as a bridge error).
        #
        # Require a NON-EMPTY parsed sender: the foreign-sender guard above
        # (`recipient and sender_addr and sender_addr != recipient`) only rejects
        # when BOTH addresses are truthy, so a reply with a missing/unparsable
        # `From` (empty sender_addr) slips past it. Gating the ping on `sender_addr`
        # means we only surface a reply whose sender we positively verified equals
        # the prospect recipient (a truthy sender that reached here must == recipient,
        # or the guard would have returned) — never an unverified "reply from someone".
        # record_reply_if_pending above is intentionally NOT gated on this (internal
        # engagement tracking keeps its pre-existing permissiveness); only the
        # owner-facing ping requires the stronger sender proof.
        if notify_owner is not None and recipient and sender_addr:
            try:
                from genesis.db.crud import marketing_prospects as _marketing_prospects

                if await _marketing_prospects.get_by_email(tracker._db, recipient) is not None:
                    await notify_owner(thread, reply)
            except Exception:
                logger.warning(
                    "Marketing reply owner-ping failed for thread %s",
                    thread.get("id"), exc_info=True,
                )

    return _bridge


def make_marketing_reply_notifier(pipeline):
    """Build the owner-ping callable for ``make_reply_engagement_bridge``'s
    ``notify_owner``. Fires ONE brief, verbatim Telegram notification to the
    owner that a real human reply landed on a cold marketing pitch.

    Owner-facing delivery is never gated: ``submit_raw`` skips governance,
    quiet-hours, and the LLM drafter. Sent ``best_effort=True`` so a
    non-DELIVERED result is logged and dropped, never deferred into the recovery
    queue (no retries, no delivery-exhausted observation) — the reply itself is
    already durably recorded in ``outreach_history`` / ``email_threads``, so a
    transient miss is an un-pushed row, not lost data.
    """

    async def _notify(thread: dict, reply) -> None:
        from genesis.outreach.types import (
            OutreachCategory,
            OutreachRequest,
            OutreachStatus,
        )

        # Attacker-controlled fields (anyone who can email the marketing inbox
        # controls their own From display name / Subject / body). Truncate RAW
        # (char count), then sanitize — see _sanitize_ping_field: the owner's
        # Telegram receives this with parse_mode="HTML" and no escaping.
        sender = _sanitize_ping_field((getattr(reply, "sender", "") or "someone")[:200])
        subject = _sanitize_ping_field((getattr(reply, "subject", "") or "").strip()[:150])
        preview = (getattr(reply, "body_preview", "") or "").strip()
        first_line = _sanitize_ping_field(
            (preview.splitlines()[0].strip() if preview else "")[:200]
        )
        text = f"📨 Reply to your pitch from {sender}"
        if subject:
            text += f"\n{subject}"
        if first_line:
            text += f'\n"{first_line}"'
        # Per-reply uniqueness so a genuine reply is never suppressed as a
        # duplicate of an earlier, differently-worded one. submit_raw dedups on
        # BOTH (signal_type, topic) AND a topic-independent content_hash(context)
        # (outreach/governance.py::_is_duplicate). content_hash only hashes the
        # FIRST 200 chars (governance.py::content_hash), so message_id must lead
        # the context — a long rendered ping (sender+subject+preview can exceed
        # 200 chars) would otherwise push the discriminator out of the hashed
        # prefix and two distinct replies would collide. Effect: each distinct
        # reply pings once; only the exact same reply re-processed is deduped. The
        # delivered message is the `text` arg (submit_raw ignores request.context,
        # which feeds only the dedup hash), so context can carry the discriminator
        # without changing what the owner sees.
        # Cap the attacker-influenceable Message-ID for storage hygiene (metadata
        # only — never rendered to the owner; feeds the dedup topic/hash), matching
        # the 200-char cap on the other reply fields.
        message_id = (getattr(reply, "message_id", "") or thread.get("id") or "")[:200]
        request = OutreachRequest(
            # MARKETING (not NOTIFICATION) so the reply-ping lands in the dedicated
            # Marketing topic like the rest of the campaign's owner updates, instead
            # of the DM that `notification` routes to. submit_raw still skips
            # governance/quiet-hours, so this stays an always-deliver owner ping.
            category=OutreachCategory.MARKETING,
            channel="telegram",
            topic=f"Marketing reply {message_id}",
            context=f"{message_id}\n{text}",
            signal_type="marketing_reply",
            salience_score=0.9,
            verbatim=True,
        )
        # best_effort: this ping is fire-and-forget. A missing Telegram adapter
        # (email-only install) or a transient outage must NOT defer into the
        # recovery queue (5 retries + a delivery-exhausted observation) — the
        # reply is already durably recorded in outreach_history/email_threads.
        result = await pipeline.submit_raw(text, request, best_effort=True)
        if result is not None and result.status == OutreachStatus.DELIVERED:
            logger.info("Pinged owner of marketing reply on thread %s", thread.get("id"))
        else:
            logger.warning(
                "Marketing reply owner-ping not delivered (status=%s) for thread %s",
                getattr(result, "status", None), thread.get("id"),
            )

    return _notify


class EngagementTracker:
    """Tracks user engagement with outreach messages."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def check_timeouts(self, timeout_hours: int = 24) -> int:
        cursor = await self._db.execute(
            "SELECT id FROM outreach_history "
            "WHERE delivered_at IS NOT NULL "
            "AND engagement_outcome IS NULL "
            "AND delivered_at < datetime('now', ? || ' hours')",
            (f"-{timeout_hours}",),
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            outreach_id = row[0] if isinstance(row, tuple) else row["id"]
            await outreach_crud.record_engagement(
                self._db, outreach_id, engagement_outcome="ignored", engagement_signal="timeout",
            )
            count += 1
        if count:
            logger.info("Marked %d outreach items as ignored (timeout)", count)
        return count

    async def find_outreach_for_reply(self, delivery_id: str) -> str | None:
        row = await outreach_crud.find_by_delivery_id(self._db, delivery_id)
        if row and row.get("engagement_outcome") is None:
            return row["id"]
        return None

    async def record_reply(self, outreach_id: str, reply_text: str) -> bool:
        """Record that the user replied to an outreach message."""
        try:
            await self._db.execute(
                "UPDATE outreach_history SET user_response = ?, "
                "engagement_outcome = 'useful', engagement_signal = 'user_reply' "
                "WHERE id = ?",
                (reply_text[:2000], outreach_id),
            )
            await self._db.commit()
            logger.info("Recorded reply engagement for outreach %s", outreach_id)
            return True
        except Exception:
            logger.warning("Failed to record reply engagement for %s", outreach_id, exc_info=True)
            return False

    async def record_reply_if_pending(self, outreach_id: str, reply_text: str) -> bool:
        """Guarded reply recording: promote NULL or a MECHANICAL marker only.

        Unlike ``record_reply`` (unconditional — Telegram/dashboard callers
        depend on its upgrade semantics), this never clobbers a human-set
        outcome: a 2nd reply, or a row marked 'acted_on'/'engaged' by the
        user, is a clean no-op. Machine-stamped states ARE overridable — the
        24h timeout ('ignored'/'timeout'), implicit-activity, and auto-digest
        markers are defaults a late real reply must beat (the reply poller
        runs 4-hourly, so 24-72h replies routinely arrive post-timeout).

        "Machine-stamped" is judged on the outcome+signal PAIRING, not the
        signal alone: the dashboard /engage route rewrites the outcome but
        leaves the old signal in place, so a human 'not_useful' can sit on a
        stale 'timeout' signal — the mechanical writers only ever pair
        ignored/ambivalent with a mechanical signal, so any other outcome is
        definitionally a human verdict and stays protected.
        Used by the mail reply→engagement bridge.
        """
        from genesis.outreach.types import MECHANICAL_ENGAGEMENT_SIGNALS_SQL_IN

        try:
            # The IN-lists are trusted module constants (sorted literals), not
            # user input — same rationale as POSITIVE_ENGAGEMENT_SQL_IN.
            cursor = await self._db.execute(
                "UPDATE outreach_history SET user_response = ?, "  # noqa: S608
                "engagement_outcome = 'useful', engagement_signal = 'user_reply' "
                "WHERE id = ? AND (engagement_outcome IS NULL "
                "OR (engagement_outcome IN ('ignored', 'ambivalent') "
                f"AND engagement_signal IN ({MECHANICAL_ENGAGEMENT_SIGNALS_SQL_IN})))",
                (reply_text[:2000], outreach_id),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.info("Recorded reply engagement for outreach %s", outreach_id)
                return True
            logger.debug(
                "Outreach %s already has an engagement outcome — reply not re-recorded",
                outreach_id,
            )
            return False
        except Exception:
            logger.warning(
                "Failed to record reply engagement for %s", outreach_id, exc_info=True,
            )
            return False

    async def record_implicit_engagement(self, outreach_id: str) -> bool:
        """Record that the user was active after receiving outreach (weak signal).

        Only upgrades NULL → ambivalent. Never downgrades engaged → ambivalent.
        """
        try:
            cursor = await self._db.execute(
                "UPDATE outreach_history SET engagement_outcome = 'ambivalent', "
                "engagement_signal = 'implicit_activity' "
                "WHERE id = ? AND engagement_outcome IS NULL",
                (outreach_id,),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.debug("Recorded implicit engagement for outreach %s", outreach_id)
                return True
            return False
        except Exception:
            logger.debug("Failed to record implicit engagement for %s", outreach_id, exc_info=True)
            return False
