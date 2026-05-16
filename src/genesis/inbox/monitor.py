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
from genesis.inbox.scanner import compute_hash, detect_changes, read_content, scan_folder
from genesis.inbox.types import CheckResult, InboxConfig, InboxItem
from genesis.security import ContentSanitizer, ContentSource

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

# URL extraction: matches https?://... and bare domain/path patterns like search.app/XYZ
_URL_RE = re.compile(
    r'(?:https?://[^\s<>\]\)]+)'
    r'|'
    r'(?:(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}/[^\s<>\]\)]+)',
    re.IGNORECASE,
)


def _extract_urls(text: str) -> list[str]:
    """Extract unique URLs from text, preserving order of first appearance."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)'\"")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


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
            # Direct domain match (original check)
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
            resume_items.append(InboxItem(
                id=row_id,
                file_path=file_path,
                content=content,
                content_hash=stored_hash,
                detected_at=str(row["created_at"]),
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
            if created_str:
                try:
                    created_dt = datetime.fromisoformat(created_str)
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
                except (ValueError, TypeError):
                    pass

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
            # Dedup: reuse existing retriable failed item instead of
            # creating a duplicate row with retry_count=0.
            existing_failed = await inbox_items.get_retriable_failed(
                self._db, str(f), max_retries=self._config.max_retries,
            )
            if existing_failed:
                logger.info(
                    "Reusing failed item %s for %s (retry_count=%d)",
                    existing_failed["id"][:8], f,
                    existing_failed["retry_count"],
                )
                await inbox_items.update_status(
                    self._db, existing_failed["id"],
                    status="pending",
                    error_message=None,
                )
                # Update content_hash so the resume pass hash-check
                # doesn't false-positive if this enters the approval gate.
                if existing_failed["content_hash"] != h:
                    await self._db.execute(
                        "UPDATE inbox_items SET content_hash = ? WHERE id = ?",
                        (h, existing_failed["id"]),
                    )
                    await self._db.commit()
                pending_items.append(InboxItem(
                    id=existing_failed["id"], file_path=str(f),
                    content=content, content_hash=h, detected_at=now_iso,
                ))
                continue

            await inbox_items.create(
                self._db,
                id=item_id,
                file_path=str(f),
                content_hash=h,
                status="pending",
                created_at=now_iso,
            )
            pending_items.append(InboxItem(
                id=item_id, file_path=str(f), content=content,
                content_hash=h, detected_at=now_iso,
            ))

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
                try:
                    last_dt = datetime.fromisoformat(last_at)
                    if now - last_dt < cooldown:
                        logger.debug(
                            "Cooldown: skipping %s (last eval %s ago)",
                            f, now - last_dt,
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
                except (ValueError, TypeError):
                    pass  # Unparseable timestamp — proceed with evaluation
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
            await inbox_items.create(
                self._db,
                id=item_id,
                file_path=str(f),
                content_hash=h,
                status="pending",
                created_at=now_iso,
            )
            pending_items.append(InboxItem(
                id=item_id, file_path=str(f), content=eval_content,
                content_hash=h, detected_at=now_iso,
            ))

        return pending_items

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
        """Build batches and dispatch to CC sessions.

        Returns the number of batches successfully dispatched.
        Appends errors to the ``errors`` list in-place.
        """
        from genesis.cc.types import (
            CCInvocation,
            CCModel,
            EffortLevel,
            SessionType,
            background_session_dir,
        )
        from genesis.db.crud import inbox_items, message_queue

        batches_dispatched = 0
        batch_size = self._config.batch_size
        mcp_path = SessionConfigBuilder().build_mcp_config("reflection")

        # Resume items as singletons first, then new/modified in batches.
        scheduled_batches: list[list[InboxItem]] = [
            [item] for item in resume_items
        ]
        for i in range(0, len(pending_items), batch_size):
            scheduled_batches.append(pending_items[i : i + batch_size])

        for batch in scheduled_batches:
            batch_id = str(uuid.uuid4())

            for item in batch:
                await inbox_items.set_batch(self._db, item.id, batch_id=batch_id)
                if item.id not in resumed_ids:
                    await inbox_items.update_status(
                        self._db, item.id, status="processing",
                    )

            prompt = self._build_prompt(batch)
            system_prompt = self._load_system_prompt()

            try:
                model = CCModel(self._config.model)
            except ValueError:
                model = CCModel.SONNET

            try:
                effort = EffortLevel(self._config.effort)
            except ValueError:
                effort = EffortLevel.MEDIUM

            invocation = CCInvocation(
                prompt=prompt,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                timeout_s=self._config.timeout_s,
                skip_permissions=True,
                disallowed_tools=["Write", "Edit", "Agent", "NotebookEdit"],
                working_dir=background_session_dir(),
                mcp_config=mcp_path,
            )

            output = None
            used_cli = True
            session_id: str | None = None
            if self._autonomous_dispatcher is not None:
                decision = await self._autonomous_dispatcher.route(
                    AutonomousDispatchRequest(
                        subsystem="inbox",
                        policy_id="inbox_evaluation",
                        action_label="inbox evaluation",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        cli_invocation=invocation,
                        api_call_site_id=None,
                        cli_fallback_allowed=True,
                        approval_required_for_cli=True,
                        approval_key_stable=True,
                        context=None,
                    ),
                )
                if decision.mode == "blocked":
                    err = f"CLI fallback blocked: {decision.reason}"
                    reason_lower = decision.reason.lower()
                    is_pending_approval = (
                        decision.approval_request_id is not None
                        and "reject" not in reason_lower
                    )
                    if is_pending_approval:
                        logger.info(
                            "Inbox batch %s parked awaiting approval %s",
                            batch_id[:8], decision.approval_request_id,
                        )
                        marker = (
                            f"{inbox_items.AWAITING_APPROVAL_PREFIX}"
                            f"{decision.approval_request_id}"
                        )
                        for item in batch:
                            await inbox_items.update_status(
                                self._db, item.id, status="processing",
                                error_message=marker, processed_at=now_iso,
                            )
                    else:
                        errors.append(err)
                        logger.warning(err)
                        for item in batch:
                            await inbox_items.update_status(
                                self._db, item.id, status="failed",
                                error_message=err, processed_at=now_iso,
                            )
                    continue
                if decision.mode == "api":
                    output = decision.output
                    used_cli = False

            if output is None:
                try:
                    sess = await self._session_manager.create_background(
                        session_type=SessionType.BACKGROUND_TASK,
                        model=model,
                        effort=effort,
                        source_tag="inbox_evaluation",
                    )
                    session_id = sess["id"]
                except Exception as exc:
                    err = f"Session creation failed: {exc}"
                    errors.append(err)
                    logger.error(err, exc_info=True)
                    for item in batch:
                        await inbox_items.update_status(
                            self._db, item.id, status="failed",
                            error_message=err, processed_at=now_iso,
                        )
                    continue

                try:
                    output = await self._invoker.run(invocation)
                except Exception as exc:
                    err = f"CC invocation failed: {exc}"
                    errors.append(err)
                    logger.error(err, exc_info=True)
                    await self._session_manager.fail(session_id, reason=err)
                    for item in batch:
                        await inbox_items.update_status(
                            self._db, item.id, status="failed",
                            error_message=err, processed_at=now_iso,
                        )
                    continue

            if output.is_error:
                err = f"CC error: {output.error_message}"
                errors.append(err)
                logger.error(err)
                if used_cli and session_id is not None:
                    await self._session_manager.fail(
                        session_id, reason=output.error_message,
                    )
                for item in batch:
                    await inbox_items.update_status(
                        self._db, item.id, status="failed",
                        error_message=err, processed_at=now_iso,
                    )
                continue

            if not output.text or not output.text.strip():
                err = "CC invocation returned empty evaluation text"
                errors.append(err)
                logger.error(
                    "Inbox batch %s returned empty text — marking as "
                    "failed (text_len=%d)",
                    batch_id[:8], len(output.text or ""),
                )
                if used_cli and session_id is not None:
                    await self._session_manager.fail(session_id, reason=err)
                for item in batch:
                    await inbox_items.update_status(
                        self._db, item.id, status="failed",
                        error_message=err, processed_at=now_iso,
                    )
                if self._event_bus:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.INBOX, Severity.ERROR,
                        "evaluation.empty_output",
                        f"Batch {batch_id[:8]} returned empty evaluation "
                        f"text (used_cli={used_cli})",
                        batch_id=batch_id, used_cli=used_cli,
                    )
                continue

            if len(batch) == 1 and _is_acknowledged(output.text):
                logger.info(
                    "Item(s) classified as Acknowledged — no response file "
                    "written (batch %s)",
                    batch_id[:8],
                )
                completed_at = self._clock().isoformat()
                for item in batch:
                    try:
                        full_content = read_content(Path(item.file_path))
                    except (FileNotFoundError, PermissionError):
                        full_content = item.content
                    if not full_content.strip():
                        full_content = item.content
                    await inbox_items.update_status(
                        self._db, item.id, status="completed",
                        processed_at=completed_at,
                        evaluated_content=full_content,
                    )
                if used_cli and session_id is not None:
                    await self._session_manager.complete(session_id)

                try:
                    source_names = ", ".join(
                        Path(item.file_path).name for item in batch
                    )
                    await message_queue.create(
                        self._db,
                        id=str(uuid.uuid4()),
                        source="cc_background",
                        target="cc_foreground",
                        message_type="finding",
                        content=(
                            f"Inbox item acknowledged (no response needed): "
                            f"{source_names}"
                        ),
                        created_at=completed_at,
                        priority="low",
                    )
                except Exception:
                    logger.exception("Failed to write message_queue entry")

                if self._event_bus:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.INBOX, Severity.INFO,
                        "check.acknowledged",
                        f"Batch {batch_id[:8]} acknowledged ({len(batch)} items)",
                        batch_id=batch_id, items=len(batch),
                    )

                batches_dispatched += 1
                continue

            # Coherence check: annotate low-quality evaluations
            batch_content = "\n".join(item.content for item in batch)
            if not _passes_coherence_check(output.text, batch_content):
                logger.warning(
                    "Inbox batch %s failed coherence check — annotating",
                    batch_id[:8],
                )
                output_text = (
                    "\u26a0\ufe0f **Low-confidence evaluation** "
                    "(failed structural coherence check)\n\n"
                    + output.text
                )
            else:
                output_text = output.text

            # Write response file
            response_path = None
            if self._writer:
                try:
                    response_path = await self._writer.write_response(
                        batch_id=batch_id,
                        source_files=[item.file_path for item in batch],
                        evaluation_text=output_text,
                        item_count=len(batch),
                    )
                except Exception as exc:
                    err = f"Response write failed: {exc}"
                    errors.append(err)
                    logger.error(err)

            url_failures = _has_url_failures(output_text, batch_content)
            if url_failures:
                logger.warning(
                    "URL failures detected in batch %s — marking as failed "
                    "to enable retry (response file still written for user)",
                    batch_id[:8],
                )

            completed_at = self._clock().isoformat()
            for item in batch:
                if url_failures:
                    await inbox_items.mark_url_failure(
                        self._db, item.id,
                        response_path=str(response_path) if response_path else None,
                        processed_at=completed_at,
                    )
                else:
                    try:
                        full_content = read_content(Path(item.file_path))
                    except (FileNotFoundError, PermissionError):
                        full_content = item.content
                    if not full_content.strip():
                        full_content = item.content
                    if response_path:
                        await inbox_items.set_response_path(
                            self._db, item.id,
                            response_path=str(response_path),
                            processed_at=completed_at,
                            evaluated_content=full_content,
                        )
                    else:
                        await inbox_items.update_status(
                            self._db, item.id, status="completed",
                            processed_at=completed_at,
                            evaluated_content=full_content,
                        )

            if used_cli and session_id is not None:
                await self._session_manager.complete(session_id)

            try:
                source_names = ", ".join(
                    Path(item.file_path).name for item in batch
                )
                await message_queue.create(
                    self._db,
                    id=str(uuid.uuid4()),
                    source="cc_background",
                    target="cc_foreground",
                    message_type="finding",
                    content=(
                        f"Inbox evaluation completed for {len(batch)} item(s): "
                        f"{source_names}. "
                        f"Response: {response_path or 'no file written'}"
                    ),
                    created_at=completed_at,
                    priority="low",
                )
            except Exception:
                logger.exception("Failed to write message_queue entry")

            if self._triage_pipeline is not None:
                from genesis.observability.types import Subsystem
                from genesis.util.tasks import tracked_task

                user_text = "\n".join(item.content for item in batch)
                tracked_task(
                    self._fire_triage(output, user_text),
                    name="inbox-triage",
                    event_bus=self._event_bus,
                    subsystem=Subsystem.INBOX,
                )

            batches_dispatched += 1

            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.INBOX, Severity.INFO,
                    "check.complete",
                    f"Batch {batch_id[:8]} evaluated ({len(batch)} items)",
                    batch_id=batch_id, items=len(batch),
                )

        return batches_dispatched

    def _build_prompt(self, items: list[InboxItem]) -> str:
        """Build the evaluation prompt from a batch of items.

        URLs are extracted from each item's content and enumerated explicitly
        so the CC session cannot silently skip them.
        """
        parts = [
            f"Evaluate the following {len(items)} inbox item(s).\n",
            "For each item, decide its type and provide a full evaluation.\n",
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


def _compute_new_content(old_content: str, new_content: str) -> str:
    """Return only the lines in new_content that weren't in old_content.

    Uses a set of non-empty stripped lines from the old content to identify
    which lines in the new content are genuinely new. Preserves order and
    blank lines between new items.
    """
    old_lines = {line.strip() for line in old_content.splitlines() if line.strip()}
    new_lines = new_content.splitlines()
    result: list[str] = []
    for line in new_lines:
        if line.strip() and line.strip() not in old_lines:
            result.append(line)
        elif not line.strip() and result:
            # Keep blank lines between new items for readability
            result.append(line)
    # Strip trailing blank lines
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)
