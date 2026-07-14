"""Alert-drain init: wire the container alert-queue drainer to the awareness tick.

F.3. Shell scripts (``tmp_watchgod.sh`` emergency tier, ``backup.sh`` failures)
and any Python caller enqueue durable alerts to ``~/.genesis/alerts/queue`` via
``genesis.guardian.alert.queue``. This drainer flushes that queue to Telegram
through the outreach pipeline every awareness tick, so an alert raised while the
channel was down is delivered when it recovers instead of vanishing.

Wired **unconditionally** (like ``cred_integrity.wire`` — NOT the guardian init,
which early-returns when ``guardian_remote.yaml`` is absent) because these alerts
matter guardian-or-not. The drainer closure resolves ``rt._outreach_pipeline``
**lazily per-tick**, so bootstrap init order is irrelevant — by the first
meaningful tick outreach is up; until then entries are kept and retried.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_QUEUE_ROOT = Path.home() / ".genesis" / "alerts" / "queue"


def wire(rt) -> None:
    """Install the per-tick alert-queue drainer on the awareness loop."""
    loop = getattr(rt, "_awareness_loop", None)
    if loop is None:
        return
    loop.set_alert_queue_drainer(_make_drainer(rt))
    logger.debug("alert-queue drainer wired to awareness tick")


def _make_drainer(rt):
    """Build the async, no-arg drainer bound to ``rt`` (lazy pipeline resolve)."""
    from genesis.guardian.alert import queue as alert_queue

    async def _send(entry: dict) -> bool:
        """Deliver one queued entry via outreach. Returns True=terminal (unlink),
        False=transient (keep + stop the drain).
        """
        pipeline = getattr(rt, "_outreach_pipeline", None)
        if pipeline is None:
            # Outreach not up yet (startup-transient) — keep and retry next tick.
            return False

        from genesis.outreach.types import (
            OutreachCategory,
            OutreachRequest,
            OutreachStatus,
        )

        source = entry.get("source", "alert")
        title = entry.get("title", "")
        body = entry.get("body", "")
        text = f"{title}\n\n{body}" if title and body else (title or body)
        # Dedup identity is the (signal_type, topic, category) triple. Derive the
        # topic from the alert's IDENTITY (dedupe_key) — NOT the source — so two
        # distinct alerts that share a source (e.g. backup-failed vs
        # offsite-failed, both source="backup") stay independently deliverable,
        # while genuine repeats of the SAME alert still collapse.
        identity = entry.get("dedupe_key") or source
        result = await pipeline.submit_raw(
            text,
            OutreachRequest(
                category=OutreachCategory.BLOCKER,
                topic=f"alert:{identity}",
                context=text,
                salience_score=1.0,
                # Constant signal_type keeps queued replays in their own dedup
                # namespace (never cross-suppressing a live guardian_alert).
                signal_type="queued_alert",
                source_id=identity,
            ),
        )
        # DELIVERED and REJECTED are both TERMINAL → unlink. REJECTED means the
        # pipeline's own dedup found it redundant; retrying would wedge the entry
        # in the queue forever. FAILED/HELD/PENDING → keep + retry next tick.
        return result.status in (OutreachStatus.DELIVERED, OutreachStatus.REJECTED)

    async def _drainer() -> None:
        drained = await alert_queue.drain(_QUEUE_ROOT, _send)
        if drained:
            logger.info("Delivered %d queued alert(s) via outreach", drained)
        alert_queue.prune(_QUEUE_ROOT)

    return _drainer
