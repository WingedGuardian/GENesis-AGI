"""One hourly page listing background sessions that STOPPED without doing their job.

Background sessions end through ``crud.cc_sessions.update_status``, which records
how each ended (``outcome``). This reader turns the stops among them (failed,
partial, blocked, expired) into at most one owner page per hour, so a campaign,
mail, peer or reflection session that dies is no longer silent. Owner-requested
work (an MCP request, an ego dispatch, a job the owner scheduled) still pages at
once from ``DirectSessionRunner``; it stamps ``outcome_paged_at`` when that send
is delivered, so this reader never pages the same stop twice, and a send that
failed falls through to the next hourly page.

State is per row (``outcome_paged_at``), not a watermark: a restart, a missed
fire or a DST change cannot skip or repeat a stop. Rate-limit parks are not
stops: they resume on their own, and ``cc.rate_limit_resume`` alerts the ones
that give up (``needs_user``) and, in ``propose_only``, the ones ready to run.
Parks left waiting while the owner has switched auto-resume ``off`` are not
paged here (a deviation from the plan's count line, owner may override):
repeating that line every hour would need state to throttle it, for a
condition the owner chose. A count of ``unreported`` sessions (clean exits with
no verdict) arrives with the self-report PR, when that number means something.

Kill switch: ``GENESIS_STOPPED_SESSION_PAGES=off`` (the reader then sends
nothing and stamps nothing, so stops page once it is turned back on, within the
lookback below). With it off, an owner-requested failure whose immediate send
also failed is not paged anywhere: neither path retries it.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import UTC, datetime, timedelta

from genesis.db.crud import cc_sessions

logger = logging.getLogger(__name__)

_ENV_KILL_SWITCH = "GENESIS_STOPPED_SESSION_PAGES"
#: How far back an unpaged stop is still worth a page: long enough to cover a
#: server that was down for days, short enough that a reader turned on late
#: does not page a month of history at once.
LOOKBACK = timedelta(days=7)
#: Most stops one page fetches; what does not fit is counted and pages next hour.
MAX_LISTED = 25
#: A stop's reason is shown as a preview; the whole text stays on the row.
_REASON_PREVIEW = 160
#: Characters one page may use, under Telegram's 4,096 (the shipped alert
#: channel, config/outreach.yaml) with room for the closing count line. An
#: install routing alerts to Discord (2,000) would have a page cut by the
#: formatter and stamped rows unseen; lower this for such an install.
PAGE_BUDGET = 3500
#: A stop this recent is left to its own immediate notifier, which stamps it a
#: few seconds after the terminal write (embedding the proposal outcome first).
GRACE = timedelta(minutes=5)


def enabled() -> bool:
    return os.environ.get(_ENV_KILL_SWITCH, "").strip().lower() not in ("off", "0", "false")


def _preview(text: str | None) -> str:
    text = " ".join((text or "").split())
    if len(text) <= _REASON_PREVIEW:
        return text
    return text[:_REASON_PREVIEW].rstrip() + " … (full reason on the session row)"


def format_page(stops: list[dict], *, total: int) -> tuple[str, list[str]] | None:
    """The page text and the ids it actually lists, or None when there is nothing.

    Lines are added only while the page stays under ``PAGE_BUDGET``: the
    channel formatter cuts an over-long message at its platform limit, and a
    row cut off there would be stamped paged without being shown. Rows left
    out stay unstamped and lead the next hour's page. Text is HTML-escaped (the
    page is sent with HTML parse mode).
    """
    if not stops:
        return None
    header = "Background session stopped" if total == 1 else f"{total} background sessions stopped"
    lines = [header, ""]
    used = len(header) + 2  # header line plus the blank line after it
    listed: list[str] = []
    for row in stops:
        what = row.get("source_tag") or row.get("session_type") or "background"
        reason = _preview(row.get("outcome_reason"))
        line = f"• {what}: {row.get('outcome')} ({row['id'][:8]})"
        line = html.escape(f"{line} - {reason}" if reason else line)
        if listed and used + len(line) + 1 > PAGE_BUDGET:
            break
        lines.append(line)
        used += len(line) + 1
        listed.append(row["id"])
    if total > len(listed):
        lines.append(f"• … and {total - len(listed)} more, listed in the next hourly page")
    return "\n".join(lines), listed


async def run_page(db, pipeline, *, now: datetime | None = None) -> int:
    """Send at most one page; return how many stops it marked paged."""
    if not enabled():
        return 0
    now = now or datetime.now(UTC)
    stops, total = await cc_sessions.list_unpaged_stops(
        db, since=(now - LOOKBACK).isoformat(), until=(now - GRACE).isoformat(), limit=MAX_LISTED
    )
    page = format_page(stops, total=total)
    if page is None:
        return 0
    text, ids = page

    from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

    result = await pipeline.submit_raw(
        text,
        OutreachRequest(
            category=OutreachCategory.ALERT,
            topic="Stopped background sessions",
            context=text,
            salience_score=0.9,
            signal_type="stopped_background_sessions",
            source_id=",".join(ids),
            # The row stamp IS the retry: a deferred redelivery would repeat
            # this page while the next hour lists the same rows again.
            defer_retry=False,
        ),
    )
    if result.status != OutreachStatus.DELIVERED:
        logger.info("stopped-session page not delivered (%s); retrying next hour", result.status)
        return 0
    return await cc_sessions.mark_outcome_paged(db, ids, ts=now.isoformat())
