"""InboxMonitor — watches a folder and dispatches content to CC for evaluation."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchRequest
from genesis.cc.session_config import SessionConfigBuilder
from genesis.inbox.scanner import (
    compute_hash,
    detect_changes,
    extract_urls,
    normalize_url_line,
    read_content,
    scan_folder,
    segment_items,
)
from genesis.inbox.types import CheckResult, InboxConfig, InboxItem
from genesis.security import ContentSanitizer, ContentSource
from genesis.util.tz import parse_utc_iso

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "identity"
_SYSTEM_PROMPT_FILE = "INBOX_EVALUATE.md"

# Max age before a pending inbox approval is considered abandoned.
# Approvals with timeout_at=None wait indefinitely by design, but the
# inbox monitor shouldn't block forever if the user never responds.
_MAX_APPROVAL_STALENESS = timedelta(hours=4)

_FALLBACK_SYSTEM_PROMPT = (
    "You are Genesis performing an inbox evaluation. "
    "Use the filename as your first classification signal — like an email subject line. "
    "Titles suggesting Genesis/AI/agents analysis get the four-lens framework "
    "(How It Helps, How It Doesn't, How It COULD, What to Learn). "
    "Titles suggesting a specific domain get analyzed in their own context. "
    "Ambiguous or 'Untitled' titles — use your best judgment based on the content. "
    "CRITICAL: When items contain URLs, you MUST attempt to fetch EVERY URL and "
    "report the result individually. Never skip URLs or say 'I have what I need.' "
    "Output readable markdown with per-item evaluation."
)

# URL extraction now lives in scanner.py (canonical). Kept as a module-level
# alias because tests and call sites import ``_extract_urls`` from monitor.
_extract_urls = extract_urls


# Patterns indicating the evaluation GAVE UP on URLs (not just encountered errors).
# Tested against all 8 existing response files: 0 false positives, 0 false negatives.
# Crucially, these do NOT include "ssl error" or "could not fetch" which appear
# in SUCCESSFUL evaluations that worked around SSL via yt-dlp/curl.
_URL_FAILURE_PATTERNS = [
    "unfetchable",
    "unreachable from this host",
    "watch them yourself",
    "cannot evaluate the video",
    "cannot assess without content",
    "could not be fetched",
    "could not be accessed",
    "i could not fetch",
    "i could not access",
]


def _has_url_failures(response_text: str, input_content: str) -> bool:
    """Detect unresolved URL fetch failures in a CC evaluation response.

    Only triggers on definitive give-up language, not on error mentions
    that may appear in successful workaround descriptions.
    """
    urls = _extract_urls(input_content)
    if not urls:
        return False
    lower = response_text.lower()
    return any(p in lower for p in _URL_FAILURE_PATTERNS)


_ACKNOWLEDGED_RE = re.compile(
    r'\*\*Classification:\*\*\s*Acknowledged',
    re.IGNORECASE,
)

# Platform name aliases for coherence check — maps bare domains to names
# that evaluations commonly use instead of the raw URL domain.
_DOMAIN_TO_NAMES: dict[str, list[str]] = {
    "linkedin.com": ["linkedin"],
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



class InboxMonitor:
    """Peripheral service that watches a folder and dispatches to CC sessions."""

    def __init__(
        self,
        *,
        db,
        invoker,
        session_manager,
        config: InboxConfig,
        writer=None,
        event_bus=None,
        clock=None,
        prompt_dir: Path | None = None,
        triage_pipeline: Callable[..., Coroutine[Any, Any, None]] | None = None,
    ):
        self._db = db
        self._invoker = invoker
        self._session_manager = session_manager
        self._config = config
        self._writer = writer
        self._event_bus = event_bus
        self._clock = clock or (lambda: datetime.now(UTC))
        self._prompt_dir = prompt_dir or _PROMPT_DIR
        self._scheduler = AsyncIOScheduler()
        self._system_prompt: str | None = None
        self._prompt_hash: str = ""
        self._prompt_version_recorded: bool = False
        self._check_lock = asyncio.Lock()
        self._triage_pipeline = triage_pipeline
        self._autonomous_dispatcher = None

    @property
    def config(self) -> InboxConfig:
        return self._config

    def set_autonomous_dispatcher(self, dispatcher: object) -> None:
        self._autonomous_dispatcher = dispatcher
        # Now that the approval gate is wired, check for items that were
        # approved while the server was down.  Use asyncio.call_later so
        # the check fires after bootstrap completes (APScheduler DateTrigger
        # is unreliable during init).
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(
                10,
                lambda: asyncio.ensure_future(self._check_inbox()),
            )
        except RuntimeError:
            pass  # No running event loop — scheduler interval will catch it

    async def start(self) -> None:
        """Start the inbox monitor scheduler."""
        self._scheduler.add_job(
            self._check_inbox,
            IntervalTrigger(seconds=self._config.check_interval_seconds),
            id="inbox_monitor_check",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.start()
        logger.info(
            "Inbox monitor started (interval=%ds, path=%s)",
            self._config.check_interval_seconds,
            self._config.watch_path,
        )

    def wake(self) -> None:
        """Schedule an immediate inbox check (one-shot).

        Called after an approval is resolved so the monitor picks up
        the approved item without waiting for the next interval tick.
        """
        from apscheduler.triggers.date import DateTrigger

        try:
            self._scheduler.add_job(
                self._check_inbox,
                DateTrigger(run_date=datetime.now(UTC)),
                id="inbox_monitor_wake",
                max_instances=1,
                replace_existing=True,
                misfire_grace_time=60,
            )
        except Exception:
            logger.debug("wake: failed to schedule immediate check", exc_info=True)

    async def stop(self) -> None:
        """Stop the inbox monitor scheduler."""
        self._scheduler.shutdown(wait=True)
        logger.info("Inbox monitor stopped")

    def _load_system_prompt(self) -> str:
        """Load and cache the system prompt from identity directory."""
        if self._system_prompt is not None:
            return self._system_prompt
        path = self._prompt_dir / _SYSTEM_PROMPT_FILE
        if path.exists():
            self._system_prompt = path.read_text()
        else:
            logger.warning("INBOX_EVALUATE.md not found at %s, using fallback", path)
            self._system_prompt = _FALLBACK_SYSTEM_PROMPT
        # Prompt versioning: record hash for outcome linkage
        from genesis.db.crud.prompt_versions import compute_prompt_hash
        self._prompt_hash = compute_prompt_hash(self._system_prompt)
        self._prompt_version_recorded = False
        return self._system_prompt

    async def check_once(self) -> CheckResult:
        """Run a single inbox check cycle. Public for testing and manual trigger."""
        if self._check_lock.locked():
            return CheckResult(errors=["Check already in progress"])
        async with self._check_lock:
            return await self._check_once_inner()

    async def _check_once_inner(self) -> CheckResult:
        """Core check logic, called under _check_lock.

        Decomposed into phase methods for readability:
        1. _phase_resume — process approval-parked items
        2. _phase_detect_changes — scan for new/modified files
        3. _phase_create_records — create DB rows for changed files
        4. _phase_dispatch_batches — build batches, route, invoke CC
        """
        from genesis.db.crud import inbox_items

        errors: list[str] = []
        watch = self._config.watch_path

        if not watch.is_dir():
            return CheckResult(errors=[f"Watch path does not exist: {watch}"])

        await inbox_items.expire_stuck_processing(self._db)

        now = self._clock()
        now_iso = now.isoformat()

        # Phase 1: Resume approval-parked items
        resume_items, resumed_ids, resumed_paths = await self._phase_resume(
            now, now_iso,
        )

        # Phase 2: Detect new/modified files
        new_files, modified_files = await self._phase_detect_changes(
            watch, resumed_paths,
        )

        if not (new_files + modified_files) and not resume_items:
            return CheckResult(
                items_found=len(scan_folder(
                    watch, self._config.response_dir,
                    recursive=self._config.recursive,
                )),
            )

        # Phase 3: Create/update DB records for changed files
        pending_items = await self._phase_create_records(
            new_files, modified_files, now, now_iso,
        )

        # Phase 4: Batch and dispatch
        batches_dispatched = await self._phase_dispatch_batches(
            resume_items, pending_items, resumed_ids, now_iso, errors,
        )

        return CheckResult(
            items_found=len(scan_folder(
                watch, self._config.response_dir,
                recursive=self._config.recursive,
            )),
            items_new=len(new_files),
            items_modified=len(modified_files),
            batches_dispatched=batches_dispatched,
            errors=errors,
        )

    # =================================================================
    # Phase 1: Resume approval-parked items
    # =================================================================

    async def _phase_resume(
        self,
        now: datetime,
        now_iso: str,
    ) -> tuple[list[InboxItem], set[str], set[str]]:
        """Resume rows parked waiting for user approval.

        Returns ``(resume_items, resumed_ids, resumed_paths)``.

        Rows stay in 'processing' state with an
        ``awaiting_approval:<request_id>`` marker in error_message.
        The resume pass ONLY dispatches on the pending→approved state
        transition.  Hash validation happens FIRST so a file that
        vanished or was edited while pending is invalidated immediately.

        Invariant: resume items are ALWAYS dispatched as singleton
        batches to preserve the original content-stable approval key.
        """
        from genesis.db.crud import inbox_items

        resume_items: list[InboxItem] = []
        awaiting_rows = await inbox_items.get_awaiting_approval(self._db)

        # Walk the dispatcher → approval_gate → approval_manager chain.
        approval_manager = None
        if self._autonomous_dispatcher is not None:
            gate = getattr(
                self._autonomous_dispatcher, "approval_gate", None,
            )
            if gate is not None:
                approval_manager = getattr(
                    gate, "approval_manager", None,
                )
            if gate is None or approval_manager is None:
                logger.error(
                    "Inbox resume pass: dispatcher is wired but "
                    "approval_gate/approval_manager accessor chain is "
                    "missing — resume pass will fall through to legacy "
                    "dispatch-every-scan behaviour. This indicates a "
                    "wiring regression; check AutonomousDispatchRouter "
                    "and AutonomousCliApprovalGate public properties.",
                )

        for row in awaiting_rows:
            row_id = str(row["id"])
            file_path = str(row["file_path"])
            stored_hash = str(row["content_hash"])
            marker = str(row.get("error_message") or "")
            request_id = marker[
                len(inbox_items.AWAITING_APPROVAL_PREFIX):
            ] if marker.startswith(inbox_items.AWAITING_APPROVAL_PREFIX) else ""

            p = Path(file_path)

            # Hash check FIRST — vanished/changed files invalidate
            # regardless of approval state.
            try:
                current_hash = compute_hash(p)
            except (FileNotFoundError, PermissionError):
                await inbox_items.update_status(
                    self._db, row_id, status="failed",
                    error_message=(
                        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}"
                        "source file vanished"
                    ),
                    processed_at=now_iso,
                )
                continue
            if current_hash != stored_hash:
                await inbox_items.update_status(
                    self._db, row_id, status="failed",
                    error_message=(
                        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}"
                        "content changed"
                    ),
                    processed_at=now_iso,
                )
                continue

            # Query approval status.
            approval_status: str | None = None
            if approval_manager is not None and request_id:
                try:
                    approval_row = await approval_manager.get_by_id(
                        request_id,
                    )
                    approval_status = (
                        str(approval_row.get("status"))
                        if approval_row else None
                    )
                except Exception:
                    logger.warning(
                        "Failed to look up approval %s for inbox row %s",
                        request_id, row_id, exc_info=True,
                    )

            if approval_status == "rejected":
                await inbox_items.update_status(
                    self._db, row_id, status="failed",
                    error_message=(
                        f"autonomous_cli_fallback rejected "
                        f"(approval {request_id})"
                    ),
                    processed_at=now_iso,
                    retry_count=self._config.max_retries,
                )
                logger.info(
                    "Inbox row %s rejected by user (approval %s) — "
                    "marked permanently failed (retry_count=%d)",
                    row_id, request_id, self._config.max_retries,
                )
                continue

            if approval_status in ("expired", "cancelled") or (
                approval_manager is not None
                and request_id
                and approval_status is None
            ):
                await inbox_items.update_status(
                    self._db, row_id, status="failed",
                    error_message=(
                        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}"
                        f"approval terminal:{approval_status or 'missing'}"
                    ),
                    processed_at=now_iso,
                )
                logger.info(
                    "Inbox row %s invalidated: approval %s in terminal state %s",
                    row_id, request_id, approval_status or "missing",
                )
                continue

            if approval_status == "pending":
                logger.debug(
                    "Inbox row %s still awaiting approval %s",
                    row_id, request_id,
                )
                continue

            # approved or legacy fall-through: load content for dispatch
            try:
                content = read_content(p)
            except (FileNotFoundError, PermissionError):
                await inbox_items.update_status(
                    self._db, row_id, status="failed",
                    error_message=(
                        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}"
                        "source file vanished"
                    ),
                    processed_at=now_iso,
                )
                continue
            # Re-dispatch the EXACT delta this batch owns (persisted in
            # batch_items at detection), NOT a full-file re-read — re-reading
            # the whole file was the bug that made an approved eval re-chew
            # every URL (Genesis-85). Legacy rows (pre-migration, no
            # batch_items) fall back to the full re-read. The hash guard above
            # ensures the file is unchanged since parking.
            batch_text = str(row.get("batch_items") or "") or content
            resume_items.append(InboxItem(
                id=row_id,
                file_path=file_path,
                content=batch_text,
                content_hash=stored_hash,
                detected_at=str(row["created_at"]),
                source_content=batch_text,
                drop_id=str(row.get("drop_id") or ""),
                approval_reqid=request_id,
            ))

        resumed_ids: set[str] = {item.id for item in resume_items}
        resumed_paths: set[str] = {item.file_path for item in resume_items}
        return resume_items, resumed_ids, resumed_paths

    # =================================================================
    # Phase 2: Detect new/modified files
    # =================================================================

    async def _scan_and_dedup(
        self, watch: Path, resumed_paths: set[str],
    ) -> tuple[list[Path], list[Path]]:
        """Scan for new/modified files and dedup against resumed paths."""
        from genesis.db.crud import inbox_items

        known = await inbox_items.get_all_known(
            self._db, max_retries=self._config.max_retries,
        )
        new_files, modified_files = detect_changes(
            watch, known, self._config.response_dir,
            recursive=self._config.recursive,
        )
        if resumed_paths:
            new_files = [f for f in new_files if str(f) not in resumed_paths]
            modified_files = [
                f for f in modified_files if str(f) not in resumed_paths
            ]
        # STORM DIAGNOSTIC (read-only): the rate-limit-window "superseded by new
        # inbox scan" churn re-detected an *unchanged* file as modified every
        # scan, for reasons not yet reproduced. Log the known-vs-current hash
        # and which row supplied the known hash so the next occurrence is
        # diagnosable. No behaviour change — this only logs.
        if modified_files:
            for f in modified_files:
                try:
                    current = compute_hash(f)
                except (FileNotFoundError, PermissionError):
                    continue
                known_hash = known.get(str(f), "<none>")
                logger.info(
                    "STORM_DIAG: %s detected modified — known_hash=%s "
                    "current_hash=%s",
                    f.name, str(known_hash)[:8], current[:8],
                )
        return new_files, modified_files

    async def _phase_detect_changes(
        self,
        watch: Path,
        resumed_paths: set[str],
    ) -> tuple[list[Path], list[Path]]:
        """Detect new and modified files in the inbox folder.

        Returns ``(new_files, modified_files)``.  When a call-site
        approval is pending, still scans for new files.  If new content
        arrives, cancels the stale approval so a fresh one reflecting
        the updated inbox state can be created.
        """
        from genesis.db.crud import inbox_items

        # Call-site gating pre-check: check if approval is pending.
        pending = None
        if self._autonomous_dispatcher is not None:
            try:
                pending = await (
                    self._autonomous_dispatcher.approval_gate.find_site_pending(
                        subsystem="inbox", policy_id="inbox_evaluation",
                    )
                )
            except Exception:
                logger.warning(
                    "find_site_pending failed for inbox_evaluation; "
                    "proceeding without pre-check",
                    exc_info=True,
                )

        if pending is not None:
            # Staleness guard: auto-cancel approvals pending longer than
            # _MAX_APPROVAL_STALENESS.  Without this, an approval with
            # timeout_at=None blocks the inbox monitor indefinitely when
            # the user never responds and no new files arrive.
            created_str = pending.get("created_at", "")
            created_dt = parse_utc_iso(created_str)
            if created_str and created_dt is None:
                logger.warning(
                    "Staleness guard: unparseable approval created_at %r; "
                    "skipping age-check this cycle (guard left intact)",
                    created_str,
                )
            if created_dt is not None:
                age = self._clock() - created_dt
                if age > _MAX_APPROVAL_STALENESS:
                    pending_id = pending.get("id")
                    logger.info(
                        "Cancelling stale inbox approval %s "
                        "(%.1fh old, threshold %.1fh)",
                        pending_id,
                        age.total_seconds() / 3600,
                        _MAX_APPROVAL_STALENESS.total_seconds() / 3600,
                    )
                    try:
                        gate = self._autonomous_dispatcher.approval_gate
                        await gate.approval_manager.cancel(pending_id)
                    except Exception:
                        logger.warning(
                            "Failed to cancel stale approval %s",
                            pending_id,
                            exc_info=True,
                        )
                    pending = None  # Cleared — proceed normally

        if pending is not None:
            # Approval pending — scan anyway to detect new content.
            new_files, modified_files = await self._scan_and_dedup(
                watch, resumed_paths,
            )
            if not new_files and not modified_files:
                logger.info(
                    "Inbox detection skipped — approval %s pending, "
                    "no new files",
                    pending.get("id"),
                )
                return [], []

            # New content while approval pending — cancel the stale
            # approval so a fresh one with updated content is created.
            pending_id = pending.get("id")
            logger.info(
                "New inbox files detected while approval %s pending — "
                "cancelling stale approval to refresh",
                pending_id,
            )
            try:
                gate = self._autonomous_dispatcher.approval_gate
                await gate.approval_manager.cancel(pending_id)
            except Exception:
                logger.warning(
                    "Failed to cancel stale inbox approval %s; "
                    "proceeding with new detection anyway",
                    pending_id,
                    exc_info=True,
                )

            # Invalidate inbox_items parked on the cancelled approval.
            try:
                awaiting = await inbox_items.get_awaiting_approval(self._db)
                for row in awaiting:
                    marker = str(row.get("error_message") or "")
                    if marker == (
                        f"{inbox_items.AWAITING_APPROVAL_PREFIX}{pending_id}"
                    ):
                        await inbox_items.update_status(
                            self._db, str(row["id"]),
                            status="failed",
                            error_message=(
                                f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}"
                                "superseded by new inbox scan"
                            ),
                            processed_at=self._clock().isoformat(),
                        )
            except Exception:
                logger.warning(
                    "Failed to invalidate parked inbox items for %s",
                    pending_id,
                    exc_info=True,
                )

            return new_files, modified_files

        # Normal path: no approval pending.
        return await self._scan_and_dedup(watch, resumed_paths)

    # =================================================================
    # Phase 3: Create DB records for changed files
    # =================================================================

    async def _phase_create_records(
        self,
        new_files: list[Path],
        modified_files: list[Path],
        now: datetime,
        now_iso: str,
    ) -> list[InboxItem]:
        """Create/update DB rows for new and modified files.

        Returns ``pending_items`` — items queued for dispatch.
        Resume items are NOT included (they're dispatched separately).
        """
        from genesis.db.crud import inbox_items

        pending_items: list[InboxItem] = []

        for f in new_files:
            item_id = str(uuid.uuid4())
            try:
                content = read_content(f)
                h = compute_hash(f)
            except (FileNotFoundError, PermissionError):
                logger.warning("File vanished before read: %s", f)
                continue
            if not content.strip():
                logger.debug("Skipping empty file: %s", f)
                await inbox_items.create(
                    self._db,
                    id=item_id,
                    file_path=str(f),
                    content_hash=h,
                    status="completed",
                    created_at=now_iso,
                )
                continue
            url_fail_count = await inbox_items.count_url_failures(
                self._db, str(f), since_hours=48,
            )
            if url_fail_count >= self._config.max_retries:
                logger.warning(
                    "Retry storm: %s has %d URL failures in 48h, skipping",
                    f, url_fail_count,
                )
                await inbox_items.create(
                    self._db,
                    id=item_id,
                    file_path=str(f),
                    content_hash=h,
                    status="completed",
                    created_at=now_iso,
                )
                continue
            # Segment the (full, for a new file) content into per-batch rows
            # under one drop. Failed batches retry via the delta path next
            # cycle (their lines stay un-baselined), so the old
            # get_retriable_failed single-row reuse is no longer needed.
            await self._queue_drop(
                str(f), content, h, now_iso, pending_items,
            )

        cooldown = timedelta(seconds=self._config.evaluation_cooldown_seconds)

        for f in modified_files:
            item_id = str(uuid.uuid4())
            try:
                content = read_content(f)
                h = compute_hash(f)
            except (FileNotFoundError, PermissionError):
                logger.warning("File vanished before read: %s", f)
                continue
            if not content.strip():
                logger.debug("Skipping empty modified file: %s", f)
                await inbox_items.create(
                    self._db,
                    id=item_id,
                    file_path=str(f),
                    content_hash=h,
                    status="completed",
                    created_at=now_iso,
                )
                continue
            last_at = await inbox_items.get_last_completed_at(
                self._db, str(f),
            )
            if last_at:
                last_dt = parse_utc_iso(last_at)
                if last_dt is None:
                    logger.warning(
                        "Cooldown check: unparseable last-eval timestamp "
                        "%r for %s; proceeding with evaluation", last_at, f,
                    )
                elif now - last_dt < cooldown:
                    # Defer WITHOUT consuming. Do NOT write a 'completed' row
                    # here: that would advance the known content hash and the
                    # modification would never be re-detected, stranding the
                    # new content until the next edit. Skipping leaves the file
                    # detectable, so the next check past the cooldown window
                    # re-detects and evaluates it.
                    logger.debug(
                        "Cooldown: deferring %s (last eval %s ago)",
                        f, now - last_dt,
                    )
                    continue
            existing = await inbox_items.get_by_file_path(self._db, str(f))
            if existing and existing["status"] == "pending":
                await inbox_items.update_status(
                    self._db, existing["id"],
                    status="failed",
                    error_message="superseded_by_modification",
                )
            prev_content = await inbox_items.get_evaluated_content(
                self._db, str(f),
            )
            if prev_content:
                delta = _compute_new_content(prev_content, content)
                if not delta.strip():
                    logger.debug(
                        "No new content in modified file: %s", f,
                    )
                    await inbox_items.create(
                        self._db,
                        id=item_id,
                        file_path=str(f),
                        content_hash=h,
                        status="completed",
                        created_at=now_iso,
                    )
                    continue
                eval_content = delta
            else:
                eval_content = content
            # Segment the delta into per-batch rows under one drop.
            await self._queue_drop(
                str(f), eval_content, h, now_iso, pending_items,
            )

        return pending_items

    async def _queue_drop(
        self,
        file_path: str,
        eval_content: str,
        content_hash: str,
        now_iso: str,
        pending_items: list[InboxItem],
    ) -> None:
        """Segment a file's delta into items, group into batches of
        ``items_per_eval``, and create one pending row per batch under a shared
        ``drop_id``. Appends one InboxItem per batch to ``pending_items``.

        Each batch's lines are stored verbatim in ``batch_items`` so the resume
        pass re-dispatches the exact delta (not a full-file re-read) and a
        restart mid-drop can reconstruct the not-yet-completed batches.
        """
        from genesis.db.crud import inbox_items

        items = segment_items(eval_content)
        if not items:
            return
        size = max(1, self._config.items_per_eval)
        batches = [items[i : i + size] for i in range(0, len(items), size)]
        # Reuse retriable failed rows for this file (one per batch) so retries
        # don't accumulate duplicate rows — the row's retry_count is preserved
        # so the permanent-failure cap still applies. (A file is re-detected for
        # retry when ALL its rows failed; partially-failed batches re-enter the
        # delta on the next file edit.)
        reusable = await inbox_items.get_retriable_failed_rows(
            self._db, file_path, max_retries=self._config.max_retries,
        )
        drop_id = str(uuid.uuid4())
        for idx, batch in enumerate(batches):
            batch_text = "\n".join(it.text for it in batch)
            if idx < len(reusable):
                row_id = str(reusable[idx]["id"])
                await inbox_items.reuse_as_pending(
                    self._db, row_id, drop_id=drop_id,
                    batch_items=batch_text, content_hash=content_hash,
                )
            else:
                row_id = str(uuid.uuid4())
                await inbox_items.create(
                    self._db,
                    id=row_id,
                    file_path=file_path,
                    content_hash=content_hash,
                    status="pending",
                    created_at=now_iso,
                    drop_id=drop_id,
                    batch_items=batch_text,
                )
            pending_items.append(InboxItem(
                id=row_id, file_path=file_path, content=batch_text,
                content_hash=content_hash, detected_at=now_iso,
                source_content=batch_text, drop_id=drop_id,
            ))

    # =================================================================
    # Phase 4: Batch and dispatch
    # =================================================================

    async def _phase_dispatch_batches(
        self,
        resume_items: list[InboxItem],
        pending_items: list[InboxItem],
        resumed_ids: set[str],
        now_iso: str,
        errors: list[str],
    ) -> int:
        """Dispatch evaluation batches to CC sessions.

        Each row is one eval-batch (<= items_per_eval items) carved from a
        file's delta; rows sharing a ``drop_id`` form one drop. Approval is
        acquired ONCE per drop (a single ``route()`` call); on approval every
        batch in the drop is dispatched directly as its own CC session, so a
        16-URL file becomes ~4 small evals + 4 response files under a single
        approval. Resume batches (their drop's approval already resolved) are
        dispatched directly and their approval is consumed once per drop.

        Returns the number of batches successfully dispatched.
        """
        from genesis.cc.types import CCModel, EffortLevel

        try:
            model = CCModel(self._config.model)
        except ValueError:
            model = CCModel.SONNET
        try:
            effort = EffortLevel(self._config.effort)
        except ValueError:
            effort = EffortLevel.MEDIUM
        system_prompt = self._load_system_prompt()
        await self._record_prompt_version(system_prompt)

        batches_dispatched = 0

        # --- Resume drops: approval already resolved -> dispatch directly. ---
        reqids_to_consume: set[str] = set()
        for _drop_id, items in self._group_by_drop(resume_items):
            for item in items:
                if await self._dispatch_one_batch(
                    item, model=model, effort=effort,
                    system_prompt=system_prompt, now_iso=now_iso, errors=errors,
                ):
                    batches_dispatched += 1
                if item.approval_reqid:
                    reqids_to_consume.add(item.approval_reqid)
        for reqid in reqids_to_consume:
            await self._consume_approval(reqid)

        # --- New drops: ONE approval per drop, then dispatch each batch. ---
        for _drop_id, items in self._group_by_drop(pending_items):
            outcome = await self._acquire_drop_approval(
                items, model=model, effort=effort,
                system_prompt=system_prompt, now_iso=now_iso, errors=errors,
            )
            if outcome != "approved":
                continue
            for item in items:
                if await self._dispatch_one_batch(
                    item, model=model, effort=effort,
                    system_prompt=system_prompt, now_iso=now_iso, errors=errors,
                ):
                    batches_dispatched += 1

        return batches_dispatched

    @staticmethod
    def _group_by_drop(
        items: list[InboxItem],
    ) -> list[tuple[str, list[InboxItem]]]:
        """Group InboxItems by drop_id, preserving first-seen order.

        Items with an empty drop_id (legacy/singleton rows) each form their own
        one-item group keyed by their row id.
        """
        groups: dict[str, list[InboxItem]] = {}
        order: list[str] = []
        for item in items:
            key = item.drop_id or f"_solo:{item.id}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        return [(k, groups[k]) for k in order]

    async def _record_prompt_version(self, system_prompt: str) -> None:
        """Record the inbox prompt version once per monitor lifetime."""
        if self._prompt_version_recorded or self._db is None:
            return
        try:
            from genesis.db.crud.prompt_versions import record_version
            await record_version(
                self._db, prompt_hash=self._prompt_hash,
                call_site="inbox_evaluate", content_preview=system_prompt[:200],
            )
            self._prompt_version_recorded = True
        except Exception:
            logger.debug("Failed to record inbox prompt version", exc_info=True)

    def _build_invocation(self, prompt: str, model, effort):
        """Build a CCInvocation for an inbox evaluation."""
        from genesis.cc.types import CCInvocation, background_session_dir
        mcp_path = SessionConfigBuilder().build_mcp_config("reflection")
        return CCInvocation(
            prompt=prompt, model=model, effort=effort,
            system_prompt=self._load_system_prompt(),
            timeout_s=self._config.timeout_s, skip_permissions=True,
            disallowed_tools=["Write", "Edit", "Agent", "NotebookEdit"],
            working_dir=background_session_dir(), mcp_config=mcp_path,
        )

    async def _acquire_drop_approval(
        self, items, *, model, effort, system_prompt, now_iso, errors,
    ) -> str:
        """Acquire ONE approval for a drop.

        Returns ``"approved"``, ``"parked"``, ``"rejected"`` or ``"failed"``.
        On park, all the drop's rows are set to processing + an awaiting marker.
        On reject/failure, all rows are failed. When no dispatcher is wired (or
        the gate is disabled, surfaced as cli_approved), returns ``"approved"``
        so the gate-OFF direct path dispatches every batch.
        """
        from genesis.db.crud import inbox_items

        # Claim the drop's rows for this cycle before the gate decision.
        batch_id = str(uuid.uuid4())
        for item in items:
            await inbox_items.set_batch(self._db, item.id, batch_id=batch_id)
            await inbox_items.update_status(
                self._db, item.id, status="processing",
            )

        if self._autonomous_dispatcher is None:
            return "approved"

        prompt = self._build_prompt(items)
        invocation = self._build_invocation(prompt, model, effort)
        decision = await self._autonomous_dispatcher.route(
            AutonomousDispatchRequest(
                subsystem="inbox", policy_id="inbox_evaluation",
                action_label="inbox evaluation",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                cli_invocation=invocation, api_call_site_id=None,
                cli_fallback_allowed=True, approval_required_for_cli=True,
                approval_key_stable=True, context=None,
            ),
        )
        if decision.mode == "blocked":
            reason_lower = decision.reason.lower()
            is_pending = (
                decision.approval_request_id is not None
                and "reject" not in reason_lower
            )
            if is_pending:
                drop_label = str(items[0].drop_id or items[0].id)[:8]
                logger.info(
                    "Inbox drop %s parked awaiting approval %s",
                    drop_label, decision.approval_request_id,
                )
                marker = (
                    f"{inbox_items.AWAITING_APPROVAL_PREFIX}"
                    f"{decision.approval_request_id}"
                )
                for item in items:
                    await inbox_items.update_status(
                        self._db, item.id, status="processing",
                        error_message=marker, processed_at=now_iso,
                    )
                return "parked"
            err = f"CLI fallback blocked: {decision.reason}"
            errors.append(err)
            logger.warning(err)
            for item in items:
                await inbox_items.update_status(
                    self._db, item.id, status="failed",
                    error_message=err, processed_at=now_iso,
                )
            return "rejected"
        if decision.mode == "api":
            # Inbox sets api_call_site_id=None so the router never hits the API
            # chain. If that invariant ever breaks, do NOT silently drop the
            # batches — fall through to direct CLI dispatch.
            logger.error(
                "Inbox drop unexpectedly received an API decision "
                "(api_call_site_id should be None) — dispatching via CLI",
            )
        return "approved"

    async def _consume_approval(self, request_id: str) -> None:
        """Consume a resume drop's approval so the next drop cannot ride the
        same (content-agnostic, stable-key) approval."""
        gate = getattr(self._autonomous_dispatcher, "approval_gate", None)
        consume = getattr(gate, "mark_consumed", None)
        if consume is None:
            return
        try:
            await consume(request_id)
        except Exception:
            logger.debug(
                "Failed to consume approval %s after resume", request_id,
                exc_info=True,
            )

    async def _dispatch_one_batch(
        self, item, *, model, effort, system_prompt, now_iso, errors,
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
        await inbox_items.set_batch(self._db, item.id, batch_id=batch_id)
        prompt = self._build_prompt([item])
        invocation = self._build_invocation(prompt, model, effort)

        session_id: str | None = None
        try:
            sess = await self._session_manager.create_background(
                session_type=SessionType.BACKGROUND_TASK,
                model=model, effort=effort, source_tag="inbox_evaluation",
            )
            session_id = sess["id"]
        except Exception as exc:
            err = f"Session creation failed: {exc}"
            errors.append(err)
            logger.error(err, exc_info=True)
            await inbox_items.update_status(
                self._db, item.id, status="failed",
                error_message=err, processed_at=now_iso,
            )
            return False

        try:
            output = await self._invoker.run(invocation)
        except Exception as exc:
            err = f"CC invocation failed: {exc}"
            errors.append(err)
            logger.error(err, exc_info=True)
            await self._session_manager.fail(session_id, reason=err)
            await inbox_items.update_status(
                self._db, item.id, status="failed",
                error_message=err, processed_at=now_iso,
            )
            return False

        if output.is_error:
            err = f"CC error: {output.error_message}"
            errors.append(err)
            logger.error(err)
            if session_id is not None:
                await self._session_manager.fail(
                    session_id, reason=output.error_message,
                )
            await inbox_items.update_status(
                self._db, item.id, status="failed",
                error_message=err, processed_at=now_iso,
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
                await self._session_manager.fail(session_id, reason=err)
            await inbox_items.update_status(
                self._db, item.id, status="failed",
                error_message=err, processed_at=now_iso,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.INBOX, Severity.ERROR, "evaluation.empty_output",
                    f"Batch {batch_id[:8]} returned empty evaluation text",
                    batch_id=batch_id,
                )
            return False

        completed_at = self._clock().isoformat()

        # Acknowledged: pure-meta note, no response file.
        if _is_acknowledged(output.text):
            logger.info(
                "Item classified as Acknowledged — no response file (batch %s)",
                batch_id[:8],
            )
            await self._complete_batch_baseline(item, completed_at)
            if session_id is not None:
                await self._session_manager.complete(session_id)
            await self._notify_batch(
                message_queue, item, completed_at,
                "Inbox item acknowledged (no response needed): "
                f"{Path(item.file_path).name}",
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.INBOX, Severity.INFO, "check.acknowledged",
                    f"Batch {batch_id[:8]} acknowledged", batch_id=batch_id,
                )
            return True

        # Coherence annotation (non-blocking).
        if not _passes_coherence_check(output.text, item.content):
            logger.warning(
                "Inbox batch %s failed coherence check — annotating",
                batch_id[:8],
            )
            output_text = (
                "⚠️ **Low-confidence evaluation** "
                "(failed structural coherence check)\n\n" + output.text
            )
        else:
            output_text = output.text

        # Response file: one per batch -> numbered Genesis-N sibling
        # (item_count=1 selects the sibling-naming path in the writer).
        response_path = None
        if self._writer:
            try:
                response_path = await self._writer.write_response(
                    batch_id=batch_id, source_files=[item.file_path],
                    evaluation_text=output_text, item_count=1,
                )
            except Exception as exc:
                err = f"Response write failed: {exc}"
                errors.append(err)
                logger.error(err)

        # Follow-ups (deduped per recommendation; non-fatal).
        if output_text:
            try:
                fu_count = await self._create_follow_ups_from_eval(
                    evaluation_text=output_text, batch_id=batch_id,
                    source_files=[item.file_path],
                )
                if fu_count:
                    logger.info(
                        "Created %d follow-up(s) from inbox eval %s",
                        fu_count, batch_id[:8],
                    )
            except Exception:
                logger.warning(
                    "Follow-up creation from inbox eval failed (non-fatal)",
                    exc_info=True,
                )

        # URL-fetch give-up -> mark failed (retry); do NOT baseline these lines.
        if _has_url_failures(output_text, item.content):
            logger.warning(
                "URL failures in batch %s — marking failed to retry "
                "(response kept)", batch_id[:8],
            )
            await inbox_items.mark_url_failure(
                self._db, item.id,
                response_path=str(response_path) if response_path else None,
                processed_at=completed_at,
            )
            if session_id is not None:
                await self._session_manager.complete(session_id)
            return False

        # Success: baseline ONLY this batch's lines.
        await self._complete_batch_baseline(
            item, completed_at, response_path=response_path,
        )
        if session_id is not None:
            await self._session_manager.complete(session_id)
        await self._notify_batch(
            message_queue, item, completed_at,
            f"Inbox evaluation completed: {Path(item.file_path).name}. "
            f"Response: {response_path or 'no file written'}",
        )
        if self._triage_pipeline is not None:
            from genesis.observability.types import Subsystem
            from genesis.util.tasks import tracked_task
            tracked_task(
                self._fire_triage(output, item.content),
                name="inbox-triage", event_bus=self._event_bus,
                subsystem=Subsystem.INBOX,
            )
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.INBOX, Severity.INFO, "check.complete",
                f"Batch {batch_id[:8]} evaluated", batch_id=batch_id,
            )
        return True

    async def _complete_batch_baseline(
        self, item, completed_at, *, response_path=None,
    ) -> None:
        """Mark a batch row completed, merging ONLY its lines into the baseline.

        The per-batch merge is what makes partial-failure safe: a failed sibling
        batch never contributes to ``evaluated_content``, so its lines reappear
        in the next delta and retry, while completed batches stay evaluated.
        """
        from genesis.db.crud import inbox_items

        source = item.source_content or item.content
        prev = await inbox_items.get_evaluated_content(self._db, item.file_path)
        full_content = _merge_evaluated_content(prev, source)
        # Diagnostic: detect the source FILE changing during evaluation by
        # comparing the drop's detection hash to the current file hash. (The
        # whole file legitimately differs from a single batch's slice now, so
        # the guard keys on the file hash, not a full-text equality check.)
        try:
            current_hash = compute_hash(Path(item.file_path))
        except (FileNotFoundError, PermissionError):
            current_hash = None
        if current_hash is not None and current_hash != item.content_hash:
            logger.warning(
                "BASELINE_GUARD: file %s changed during eval "
                "(detection_hash=%s current_hash=%s)",
                item.file_path, item.content_hash[:8], current_hash[:8],
            )
            if self._event_bus:
                try:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.INBOX, Severity.WARNING,
                        "baseline_guard.file_changed",
                        f"File changed during evaluation: {item.file_path}",
                        file_path=item.file_path,
                    )
                except Exception:
                    logger.debug(
                        "BASELINE_GUARD event emit failed", exc_info=True,
                    )
        if response_path:
            await inbox_items.set_response_path(
                self._db, item.id, response_path=str(response_path),
                processed_at=completed_at, evaluated_content=full_content,
            )
        else:
            await inbox_items.update_status(
                self._db, item.id, status="completed",
                processed_at=completed_at, evaluated_content=full_content,
            )

    async def _notify_batch(
        self, message_queue, item, completed_at, content: str,
    ) -> None:
        """Write a cc_background -> cc_foreground finding for a dispatched batch."""
        try:
            await message_queue.create(
                self._db, id=str(uuid.uuid4()), source="cc_background",
                target="cc_foreground", message_type="finding",
                content=content, created_at=completed_at, priority="low",
            )
        except Exception:
            logger.exception("Failed to write message_queue entry")

    def _build_prompt(self, items: list[InboxItem]) -> str:
        """Build the evaluation prompt from a batch of items.

        URLs are extracted from each item's content and enumerated explicitly
        so the CC session cannot silently skip them.
        """
        parts = [
            f"Evaluate the following {len(items)} inbox item(s).\n",
            "For each item, decide its type and provide a full evaluation.\n",
            (
                "⚠️ **DELTA EVALUATION** — The content below contains ONLY new "
                "items added since the last evaluation. Do NOT use the Read tool "
                "to open the source inbox file. Do NOT re-evaluate items that are "
                "not listed below. Evaluate ONLY the content provided here.\n"
            ),
        ]
        for idx, item in enumerate(items, 1):
            name = Path(item.file_path).name
            urls = _extract_urls(item.content)
            parts.append(f"\n---\n\n## Item {idx}: {name}\n")
            if urls:
                parts.append(
                    "\n### URLs found (you MUST attempt to fetch each one "
                    "and report the result):\n",
                )
                for i, url in enumerate(urls, 1):
                    parts.append(f"{i}. {url}")
                parts.append("")  # blank line separator
            _sanitizer = ContentSanitizer()
            result = _sanitizer.sanitize(item.content, ContentSource.INBOX)
            if result.detected_patterns:
                logger.warning(
                    "Injection patterns detected in inbox item %s: %s (risk=%.2f)",
                    name, result.detected_patterns, result.risk_score,
                )
            parts.append(f"\n### Content:\n\n{result.wrapped}\n")
        return "\n".join(parts)

    @staticmethod
    def _compute_new_content(old_content: str, new_content: str) -> str:
        """Return only the lines in new_content that weren't in old_content."""
        return _compute_new_content(old_content, new_content)

    async def _fire_triage(self, output: Any, user_text: str) -> None:
        """Fire-and-forget triage pipeline. Never crashes inbox processing."""
        try:
            await self._triage_pipeline(output, user_text, "inbox")
        except Exception:
            logger.exception("Inbox triage pipeline failed (non-fatal)")

    async def _check_inbox(self) -> None:
        """Scheduled callback — wraps check_once with error handling."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Inbox check skipped (Genesis paused)")
                return
        except Exception:
            logger.debug("GenesisRuntime paused check failed", exc_info=True)
        try:
            result = await self.check_once()
            if result.errors:
                logger.warning(
                    "Inbox check completed with %d error(s): %s",
                    len(result.errors), result.errors,
                )
            elif result.batches_dispatched > 0:
                logger.info(
                    "Inbox check: %d new, %d modified, %d batches dispatched",
                    result.items_new, result.items_modified, result.batches_dispatched,
                )
            else:
                logger.debug(
                    "Inbox check: %d files scanned, no changes detected",
                    result.items_found,
                )
            # Heartbeat
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.INBOX, Severity.DEBUG,
                    "heartbeat", "inbox_monitor check completed",
                )
        except Exception:
            logger.exception("Inbox check failed")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.INBOX, Severity.ERROR,
                    "check.failed",
                    "Inbox check failed with exception",
                )

    # ------------------------------------------------------------------
    # Follow-up creation from evaluation Recommendation blocks
    # ------------------------------------------------------------------

    # Classification → (strategy, priority, pinned)
    _ACTION_MAP: dict[str, tuple[str, str, bool]] = {
        "adopt":  ("user_input_needed", "high",   True),
        "adapt":  ("user_input_needed", "medium", True),
        "watch":  ("ego_judgment",      "low",    False),
        "explore": ("user_input_needed", "medium", True),
        "bookmark": ("ego_judgment",     "low",    False),
    }

    async def _create_follow_ups_from_eval(
        self,
        evaluation_text: str,
        batch_id: str,
        source_files: list[str],
    ) -> int:
        """Parse Recommendation blocks and create follow-ups for actionable items.

        Returns the number of follow-ups created.
        """
        import hashlib

        from genesis.db.crud import follow_ups
        from genesis.inbox.recommendation import parse_recommendations

        recs = parse_recommendations(evaluation_text)
        created = 0
        source_name = ", ".join(Path(f).name for f in source_files)

        for rec in recs:
            if not rec.is_actionable:
                continue

            action_key = rec.action.lower().replace("_", " ").strip()
            mapping = self._ACTION_MAP.get(action_key)
            if mapping is None:
                logger.debug(
                    "Unmapped action '%s' — skipping follow-up", rec.action,
                )
                continue

            strategy, priority, pinned = mapping

            title = rec.item_title or "Untitled"
            content = f"[{rec.action.upper()}] {title}: {rec.next_step}"
            reason = (
                f"Inbox evaluation {batch_id[:8]}: {source_name}. "
                f"Confidence: {rec.confidence}. Effort: {rec.effort}."
            )

            # Dedup: skip if an identical recommendation already exists so that
            # re-evaluating the same URL (or overlapping drops) never piles up
            # duplicate follow-up rows. Key on the item's primary URL
            # (tracking-normalized) or title + the next_step.
            urls_in_title = extract_urls(title)
            primary = (
                normalize_url_line(urls_in_title[0])
                if urls_in_title else title.strip().lower()
            )
            dedup_key = hashlib.sha256(
                f"inbox_evaluation|{primary}|"
                f"{(rec.next_step or '').strip().lower()}".encode()
            ).hexdigest()
            if await follow_ups.exists_by_dedup_key(self._db, dedup_key):
                logger.debug("Skipping duplicate inbox follow-up: %s", title)
                continue

            await follow_ups.create(
                self._db,
                content=content,
                source="inbox_evaluation",
                reason=reason,
                strategy=strategy,
                priority=priority,
                pinned=pinned,
                # The evaluator already judges each item genesis-vs-user; reuse it.
                domain="internal" if rec.classification == "genesis" else "user_world",
                dedup_key=dedup_key,
            )
            created += 1

        return created


def _compute_new_content(old_content: str, new_content: str) -> str:
    """Return only the lines in new_content that weren't in old_content.

    Uses a set of non-empty stripped lines from the old content to identify
    which lines in the new content are genuinely new. Preserves order and
    blank lines between new items.

    Lines are compared after URL tracking-param normalization, so the same
    article re-pasted with different share/tracking params is not treated as
    new. The original (un-normalized) line is kept in the output for evaluation.
    """
    old_lines = {
        normalize_url_line(line.strip())
        for line in old_content.splitlines()
        if line.strip()
    }
    new_lines = new_content.splitlines()
    result: list[str] = []
    for line in new_lines:
        stripped = line.strip()
        if stripped and normalize_url_line(stripped) not in old_lines:
            result.append(line)
        elif not stripped and result:
            # Keep blank lines between new items for readability
            result.append(line)
    # Strip trailing blank lines
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


def _merge_evaluated_content(
    prev_content: str | None, source_content: str,
) -> str:
    """Merge previous baseline with detection-time content.

    Returns the union of all non-empty stripped lines from both inputs,
    sorted for deterministic output.  This makes ``evaluated_content``
    monotonically grow — once a line has been evaluated it stays in the
    baseline forever, preventing re-evaluation even if the source file
    is cleared and refilled by sync (e.g. rclone from Dropbox).
    """
    lines: set[str] = set()
    if prev_content:
        lines.update(
            line.strip() for line in prev_content.splitlines() if line.strip()
        )
    lines.update(
        line.strip() for line in source_content.splitlines() if line.strip()
    )
    return "\n".join(sorted(lines))
