"""Execute and post-process one approved inbox evaluation batch."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.cc.exceptions import CCNetworkOfflineError
from genesis.inbox.scanner import extract_urls as _extract_urls
from genesis.inbox.url_coverage import (
    _coverage_url_label,
    _has_url_failures,
    _uncovered_urls,
)

if TYPE_CHECKING:
    from genesis.inbox.monitor import InboxMonitor

# Keep the existing category so log filters survive the extraction.
logger = logging.getLogger("genesis.inbox.monitor")


_ACKNOWLEDGED_RE = re.compile(
    r"\*\*Classification:\*\*\s*Acknowledged",
    re.IGNORECASE,
)

# Platform name aliases for coherence check — maps bare domains to names
# that evaluations commonly use instead of the raw URL domain.
_DOMAIN_TO_NAMES: dict[str, list[str]] = {
    "linkedin.com": ["linkedin"],
    "lnkd.in": ["linkedin"],
    "github.com": ["github"],
    "youtube.com": ["youtube"],
    "youtu.be": ["youtube"],
    "medium.com": ["medium"],
    "twitter.com": ["twitter", "x.com", "x/twitter"],
    "x.com": ["twitter", "x.com", "x/twitter"],
    "reddit.com": ["reddit"],
    "arxiv.org": ["arxiv"],
    "huggingface.co": ["hugging face", "huggingface"],
    "producthunt.com": ["product hunt", "producthunt"],
    "news.ycombinator.com": ["hacker news", "ycombinator", "hn"],
    "substack.com": ["substack"],
}


def _is_acknowledged(response_text: str) -> bool:
    """Detect if the LLM classified this item as Acknowledged (no response needed).

    The LLM uses ``**Classification:** Acknowledged`` when a note is pure
    meta-context — e.g. a file that contains only ``[This notepad is for
    genesis items]`` with no body, or ``[Just archiving this for context,
    no action needed]``.  Acknowledged items absorb context but produce no
    response file.

    Do NOT confuse this with ``[This note is USER specific ...]`` — that
    bracket is a classification directive for real content (apply the
    User framework), not a trigger for Acknowledged routing.
    """
    return bool(_ACKNOWLEDGED_RE.search(response_text))


def _passes_coherence_check(evaluation: str, source_content: str) -> bool:
    """Structural coherence check on inbox evaluation output.

    Returns True if the evaluation meets minimum structural expectations
    from the INBOX_EVALUATE.md system prompt. False triggers an annotation
    but does not block writing the response.
    """
    if not evaluation or len(evaluation.strip()) < 300:
        return False  # Too short for any real evaluation

    # Must contain expected structural marker
    if "# Inbox Evaluation" not in evaluation:
        return False

    # Source URLs should appear in evaluation (domain-level or platform-name check).
    # Evaluations often use platform names ("LinkedIn") rather than raw domains
    # ("www.linkedin.com"), so we check both.
    urls = re.findall(r"https?://([^\s/]+)", source_content)
    if urls:
        eval_lower = evaluation.lower()
        matched = False
        for u in urls:
            domain = u.lower()
            # Direct domain match
            if domain in eval_lower:
                matched = True
                break
            # Platform-name match: strip www., look up known names
            bare = domain.removeprefix("www.")
            names = _DOMAIN_TO_NAMES.get(bare, [])
            if any(name in eval_lower for name in names):
                matched = True
                break
            # Fallback: use bare domain stem (e.g. "linkedin" from "linkedin.com")
            stem = bare.split(".")[0]
            if len(stem) > 3 and stem in eval_lower:
                matched = True
                break
        if not matched:
            return False  # Evaluation doesn't reference ANY source URLs

    return True


async def run_one_batch(
    monitor: InboxMonitor,
    item,
    *,
    model,
    effort,
    system_prompt,
    now_iso,
    errors,
) -> bool:
    """Run one eval-batch as its own CC session and post-process the result.

    Approval is already cleared at the drop level, so this dispatches
    directly (create_background + invoker.run). On success it merges ONLY
    this batch's lines (``item.source_content``) into the file's
    ``evaluated_content`` baseline, so a failed sibling batch's lines stay
    un-baselined and resurface in the next delta for retry. Returns True iff
    the batch produced a completed/acknowledged result.
    """
    from genesis.cc.types import SessionType
    from genesis.db.crud import inbox_items, message_queue

    batch_id = str(uuid.uuid4())
    await inbox_items.set_batch(monitor._db, item.id, batch_id=batch_id)
    prompt = monitor._build_prompt([item])
    invocation = monitor._build_invocation(prompt, model, effort, system_prompt)

    session_id: str | None = None
    try:
        sess = await monitor._session_manager.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=model,
            effort=effort,
            source_tag="inbox_evaluation",
            # WS-3: inbox sessions evaluate EXTERNAL mail content — same
            # origin the CCInvocation env stamp uses (_build_invocation).
            origin="external_untrusted",
        )
        session_id = sess["id"]
    except Exception as exc:
        err = f"Session creation failed: {exc}"
        errors.append(err)
        logger.error(err, exc_info=True)
        await inbox_items.update_status(
            monitor._db,
            item.id,
            status="failed",
            error_message=err,
            processed_at=now_iso,
        )
        return False

    try:
        output = await monitor._invoker.run(invocation)
    except CCNetworkOfflineError as exc:
        # #1766 (inbox leg): the network being down is not this item's
        # failure. Fail the row so the retry lane picks it up once
        # connectivity returns, but keep its retry budget — the default
        # failed-path increment turns a ~90-minute outage into permanently
        # parked items (3 retries x 30-minute scans).
        err = f"CC invocation deferred, network offline: {exc}"
        errors.append(err)
        logger.warning(err)
        await monitor._session_manager.fail(session_id, reason=err)
        # retriable_below: an approved row resumed from a parked approval
        # can carry a count from an older, higher cap; an outage must not
        # land it at the current one (#2447 review).
        await inbox_items.mark_failed_keeping_retries(
            monitor._db,
            item.id,
            error_message=err,
            processed_at=now_iso,
            retriable_below=monitor._config.max_retries,
        )
        return False
    except Exception as exc:
        err = f"CC invocation failed: {exc}"
        errors.append(err)
        logger.error(err, exc_info=True)
        await monitor._session_manager.fail(session_id, reason=err)
        await inbox_items.update_status(
            monitor._db,
            item.id,
            status="failed",
            error_message=err,
            processed_at=now_iso,
        )
        return False

    if output.is_error:
        err = f"CC error: {output.error_message}"
        errors.append(err)
        logger.error(err)
        if session_id is not None:
            await monitor._session_manager.fail(
                session_id,
                reason=output.error_message,
            )
        await inbox_items.update_status(
            monitor._db,
            item.id,
            status="failed",
            error_message=err,
            processed_at=now_iso,
        )
        return False

    if not output.text or not output.text.strip():
        err = "CC invocation returned empty evaluation text"
        errors.append(err)
        logger.error(
            "Inbox batch %s returned empty text — marking failed",
            batch_id[:8],
        )
        if session_id is not None:
            await monitor._session_manager.fail(session_id, reason=err)
        await inbox_items.update_status(
            monitor._db,
            item.id,
            status="failed",
            error_message=err,
            processed_at=now_iso,
        )
        if monitor._event_bus:
            from genesis.observability.types import Severity, Subsystem

            await monitor._event_bus.emit(
                Subsystem.INBOX,
                Severity.ERROR,
                "evaluation.empty_output",
                f"Batch {batch_id[:8]} returned empty evaluation text",
                batch_id=batch_id,
            )
        return False

    completed_at = monitor._clock().isoformat()

    # Acknowledged: pure-meta note, no response file. Honored ONLY for
    # URL-free items — a URL-bearing item claiming Acknowledged would
    # baseline its URLs with zero coverage evidence (the silent-loss
    # class the coverage gate below exists to close), so it falls
    # through to the normal path and its gates instead.
    if _is_acknowledged(output.text) and not _extract_urls(item.content):
        logger.info(
            "Item classified as Acknowledged — no response file (batch %s)",
            batch_id[:8],
        )
        await monitor._complete_batch_baseline(item, completed_at)
        if session_id is not None:
            await monitor._session_manager.complete(session_id)
        await monitor._notify_batch(
            message_queue,
            item,
            completed_at,
            f"Inbox item acknowledged (no response needed): {Path(item.file_path).name}",
        )
        if monitor._event_bus:
            from genesis.observability.types import Severity, Subsystem

            await monitor._event_bus.emit(
                Subsystem.INBOX,
                Severity.INFO,
                "check.acknowledged",
                f"Batch {batch_id[:8]} acknowledged",
                batch_id=batch_id,
            )
        return True

    # Coherence annotation (non-blocking).
    if not _passes_coherence_check(output.text, item.content):
        logger.warning(
            "Inbox batch %s failed coherence check — annotating",
            batch_id[:8],
        )
        output_text = (
            "⚠️ **Low-confidence evaluation** (failed structural coherence check)\n\n" + output.text
        )
    else:
        output_text = output.text

    # Response file: one per batch -> numbered Genesis-N sibling
    # (item_count=1 selects the sibling-naming path in the writer).
    response_path = None
    if monitor._writer:
        try:
            response_path = await monitor._writer.write_response(
                batch_id=batch_id,
                source_files=[item.file_path],
                evaluation_text=output_text,
                item_count=1,
            )
        except Exception as exc:
            err = f"Response write failed: {exc}"
            errors.append(err)
            logger.error(err)

    # URL-fetch give-up -> mark failed (retry); do NOT baseline these lines.
    if _has_url_failures(output_text, item.content):
        logger.warning(
            "URL failures in batch %s — marking failed to retry (response kept)",
            batch_id[:8],
        )
        await inbox_items.mark_url_failure(
            monitor._db,
            item.id,
            response_path=str(response_path) if response_path else None,
            processed_at=completed_at,
        )
        if session_id is not None:
            await monitor._session_manager.complete(session_id)
        return False

    # Coverage gate: a URL the response never MENTIONS emitted no give-up
    # language, so the check above cannot see it. Do NOT baseline the
    # batch — re-queue through the same partial-failure retry path
    # (max_retries-capped), so silent omission is a retry, never a
    # permanent invisible loss.
    uncovered = _uncovered_urls(output_text, item.content)
    if uncovered:
        # Stable opaque ids keep presigned query values and URL userinfo out
        # of the journal and inbox_items.error_message while leaving each
        # miss correlatable across retries. Bound the stored message too.
        shown = ", ".join(_coverage_url_label(url) for url in uncovered[:5])
        if len(uncovered) > 5:
            shown += f" (+{len(uncovered) - 5} more)"
        if monitor._config.url_coverage_mode != "enforce":
            # SHADOW (opt-in): the verdict is computed and recorded, and
            # nothing acts on it. This was the shipped default until the
            # **Source:** contract's compliance was measured — 0 of the first
            # 42 evaluations under it would have re-queued — and until parking
            # alerted the owner (see _dispatch_one_batch / _alert_parked).
            # Kept so an install can observe the gate without acting on it.
            logger.warning(
                "url-coverage SHADOW: batch %s would have re-queued %d uncovered URL(s): %s",
                batch_id[:8],
                len(uncovered),
                shown,
            )
        else:
            logger.warning(
                "Batch %s response covers no trace of %d URL(s) — "
                "marking failed to retry (response kept): %s",
                batch_id[:8],
                len(uncovered),
                shown,
            )
            await inbox_items.mark_url_failure(
                monitor._db,
                item.id,
                response_path=str(response_path) if response_path else None,
                processed_at=completed_at,
                error_message="partial_url_failure: uncovered " + shown,
            )
            if session_id is not None:
                await monitor._session_manager.complete(session_id)
            return False

    # Follow-ups + build lane fire only for evaluations that passed their
    # gates — a coverage-failed eval retries, and acting on it here
    # would create rows from an evaluation we just declared unevaluated
    # (dedup would then block the retry's corrected verdict).
    if output_text:
        try:
            fu_count = await monitor._create_follow_ups_from_eval(
                evaluation_text=output_text,
                batch_id=batch_id,
                source_files=[item.file_path],
                # The CC-generated id of the evaluation session — the
                # tracker below's FIRST preference only (output.session_id,
                # the id transcript tracing keys on). Its second fallback,
                # the internal manager id, is deliberately NOT taken here:
                # wrong namespace for this column, and a substitute id is
                # forbidden. None when absent.
                source_session=getattr(output, "session_id", None) or None,
            )
            if fu_count:
                logger.info(
                    "Created %d follow-up(s) from inbox eval %s",
                    fu_count,
                    batch_id[:8],
                )
        except Exception:
            logger.warning(
                "Follow-up creation from inbox eval failed (non-fatal)",
                exc_info=True,
            )

        # Capability-build lane (non-fatal, no-op unless enabled + wired):
        # consumes `build` verdicts into greenlight cards. While it is live,
        # BUILD verdicts never become follow-ups; when it is unwired or
        # disabled, _create_follow_ups_from_eval surfaces them as follow-ups
        # instead (see _BUILD_FALLBACK_MAP).
        if monitor._build_lane is not None:
            try:
                await monitor._build_lane.handle_eval(
                    evaluation_text=output_text,
                    batch_id=batch_id,
                    item=item,
                    response_path=response_path,
                )
            except Exception:
                logger.warning(
                    "Build-lane eval handling failed (non-fatal)",
                    exc_info=True,
                )
            # Runs even when handle_eval raised part-way: retirement keys on
            # a candidate row EXISTING, so it retires exactly what the lane
            # did consume and nothing it did not.
            try:
                await monitor._retire_build_fallbacks_owned_by_lane(output_text)
            except Exception:
                logger.warning(
                    "Retiring lane-off BUILD fallback rows failed (non-fatal)",
                    exc_info=True,
                )

    # Success: baseline ONLY this batch's lines, after its synchronous
    # durable side effects. A cancellation or process exit before this point
    # leaves the row non-completed so recovery can retry the missing writes.
    await monitor._complete_batch_baseline(
        item,
        completed_at,
        response_path=response_path,
    )
    if session_id is not None:
        await monitor._session_manager.complete(session_id)

    await monitor._notify_batch(
        message_queue,
        item,
        completed_at,
        f"Inbox evaluation completed: {Path(item.file_path).name}. "
        f"Response: {response_path or 'no file written'}",
    )
    if monitor._triage_pipeline is not None:
        from genesis.observability.types import Subsystem
        from genesis.util.tasks import tracked_task

        tracked_task(
            monitor._fire_triage(output, item.content),
            name="inbox-triage",
            event_bus=monitor._event_bus,
            subsystem=Subsystem.INBOX,
        )
    # Deterministic memory persistence over the curated output text. Detached
    # and isolated — fires AFTER baseline+complete so it can never affect the
    # batch. No-op unless router+store are wired (see __init__).
    if output_text and monitor._router is not None and monitor._memory_store is not None:
        from genesis.observability.types import Subsystem
        from genesis.util.tasks import tracked_task

        # Prefer the CC-generated session id (output.session_id) for
        # source_session_id — that's what transcript tracing and every other
        # extraction path key on (extraction_job uses cc_session_id). Fall
        # back to the internal cc_sessions.id lifecycle UUID if absent.
        cc_sid = getattr(output, "session_id", "") or session_id
        tracked_task(
            monitor._persist_eval_memories(output_text, batch_id, cc_sid, [item.file_path]),
            name="inbox-eval-memory",
            event_bus=monitor._event_bus,
            subsystem=Subsystem.INBOX,
        )
    if monitor._event_bus:
        from genesis.observability.types import Severity, Subsystem

        await monitor._event_bus.emit(
            Subsystem.INBOX,
            Severity.INFO,
            "check.complete",
            f"Batch {batch_id[:8]} evaluated",
            batch_id=batch_id,
        )
    return True
