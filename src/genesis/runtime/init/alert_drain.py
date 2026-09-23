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

logger = logging.getLogger(__name__)


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
                # This drain IS the durable retry (14-day file queue, retried
                # every awareness tick). The pipeline must not ALSO defer a
                # retry to the recovery worker — its whole budget is ~82
                # minutes before it discards the row, so handing ownership
                # over would trade this queue's durability away, and keeping
                # both owners double-delivers (issue #1781).
                defer_retry=False,
            ),
        )
        # DELIVERED, REJECTED and HELD are all TERMINAL → unlink.
        #  - REJECTED = the pipeline's own dedup found it redundant; retrying
        #    would wedge the entry in the queue forever.
        #  - HELD = the WS-8 email autonomy gate recorded a durable pending row
        #    (`pipeline._deliver`, OutreachStatus.HELD) and its resolution
        #    watcher owns delivery from there. `submit_raw`'s dedup consults
        #    DELIVERED history only, so it does not suppress a second HOLD:
        #    keeping the entry mints a fresh pending row every tick, and
        #    approving them delivers the same alert once per hold. Terminal here
        #    matches `resilience/outreach_recovery.py:170`, which already treats
        #    HELD as terminal — and whose comment claims it "mirrors
        #    alert_drain". It did not; HELD and IGNORED here now match recovery's terminal set.
        # FAILED/PENDING → keep + retry next tick, and because the request above
        # carries `defer_retry=False`, a FAILED send leaves THIS queue as the
        # delivery's ONLY retrier. Before that flag existed the pipeline also
        # deferred its own retry, and two independent retriers owned one
        # delivery: recovery delivered at 08:31:57 and the kept entry resent at
        # 08:35:42 — one OOM alert, two pages (issue #1781, MEASURED from the
        # journal + outreach rows).
        # IGNORED is terminal too. The pipeline chose that status SPECIFICALLY
        # to stop retry loops (its own comment: "the drain treats
        # DELIVERED/ENGAGED/HELD/IGNORED as terminal and FAILED as transient"),
        # and outreach_recovery already discards it as a permanent
        # non-delivery. Honouring it here matters on an install whose blocker
        # channel has no registered adapter: every queued alert resolves
        # IGNORED forever, which without this line meant an ERROR log per
        # entry per tick until the queue's 14-day prune silently discarded the
        # alerts with no record that they were never delivered.
        terminal = result.status in (
            OutreachStatus.DELIVERED,
            OutreachStatus.REJECTED,
            OutreachStatus.HELD,
            OutreachStatus.IGNORED,
        )
        # A TERMINAL NON-DELIVERY IS NOT A SUCCESS, and until now the drain
        # could not tell the operator apart from one: `queue.drain` unlinks on
        # True and says nothing, so a discarded alert and a delivered one left
        # the same trace — none.
        #
        # This is the honest cost of treating IGNORED as terminal. Review
        # objected that a missing adapter now DROPS the alert where before a
        # configuration fix could still recover it, and the mechanism is real.
        # The old behaviour did not preserve it either, though: it retried
        # forever, logged an ERROR per entry per tick, and the queue's 14-day
        # prune deleted it anyway — recovery only if someone noticed the flood
        # inside the window. So the choice is not loss-versus-recovery, it is
        # a silent loss in 14 days versus a loud one now, and `outreach_
        # recovery` already chose loud: it calls `mark_discarded` with a
        # reason for this same status. The alert queue has no such call, so
        # the log is where the disposition gets recorded.
        if terminal and result.status in (OutreachStatus.REJECTED, OutreachStatus.IGNORED):
            logger.warning(
                "alert-queue entry discarded UNDELIVERED (%s): source=%s dedupe_key=%s%s — "
                "the alert is gone; if this is a channel misconfiguration, fix it and the "
                "NEXT alert will deliver, but this one will not be retried",
                result.status.value,
                source,
                identity,
                f" error={result.error}" if result.error else "",
            )
        return terminal

    async def _drainer() -> None:
        # Resolve at drain time (call-time) via the GENESIS_HOME-aware resolver,
        # so the queue root matches what watchdog writes and the test suite can
        # isolate both through one seam (_isolate_alert_queue).
        from genesis.env import alert_queue_root

        root = alert_queue_root()
        drained = await alert_queue.drain(root, _send)
        if drained:
            logger.info("Delivered %d queued alert(s) via outreach", drained)
        alert_queue.prune(root)

    return _drainer
