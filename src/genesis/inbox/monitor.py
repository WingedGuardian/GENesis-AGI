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
        """Core check logic, called under _check_lock."""
        from genesis.cc.types import (
            CCInvocation,
            CCModel,
            EffortLevel,
            SessionType,
            background_session_dir,
        )
        from genesis.db.crud import inbox_items, message_queue

        errors: list[str] = []
        watch = self._config.watch_path

        if not watch.is_dir():
            return CheckResult(errors=[f"Watch path does not exist: {watch}"])

        # Expire stuck processing items before loading known set.
        # Rows carrying an ``awaiting_approval:*`` marker are deliberately
        # skipped by expire_stuck_processing — see the CRUD function for
        # rationale.
        await inbox_items.expire_stuck_processing(self._db)

        now = self._clock()
        now_iso = now.isoformat()

        # 1. Resume rows that are parked waiting for user approval.
        # Rows stay in 'processing' state with an
        # ``awaiting_approval:<request_id>`` marker in error_message.
        #
        # The resume pass ONLY dispatches on the pending→approved state
        # transition.  Prior iterations of this code re-dispatched on
        # every scan and relied on the approval gate's stable key to
        # dedup.  That worked but generated wasted dispatch cycles
        # while pending.  With the per-call-site gating in T10, we now:
        #
        #   1. Parse approval_request_id from the marker
        #   2. Look up approval status via approval_manager.get_by_id
        #   3. Route by status:
        #      - approved  → validate content + queue for dispatch
        #      - rejected  → mark row failed with rejection message
        #      - pending   → leave row alone (no dispatch, no Telegram)
        #      - expired / cancelled / NULL → mark row failed with the
        #        APPROVAL_INVALIDATED_PREFIX marker so the next scan
        #        treats the file as new and starts fresh
        #
        # Hash validation still happens FIRST so a file that vanished
        # or was edited while pending is invalidated immediately —
        # regardless of whether the approval ever resolved.
        #
        # Invariant: resume items are ALWAYS dispatched as singleton
        # batches (one item per dispatch call), regardless of
        # self._config.batch_size.  This preserves the original
        # content-stable approval key.
        resume_items: list[InboxItem] = []
        awaiting_rows = await inbox_items.get_awaiting_approval(self._db)

        # Reach the ApprovalManager via public accessors so wrappers /
        # test doubles that only mirror public API still work.  Silent
        # fall-through here would cause the resume pass to regress to
        # "re-dispatch every scan" behaviour without anyone noticing —
        # that's the exact wasted-work pattern T11 was meant to eliminate.
        # If we find the dispatcher but cannot walk the chain, log an
        # ERROR so the wiring bug is visible in observability immediately.
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

            # Query approval status.  If we can't reach the approval
            # manager at all (dispatcher not yet wired, startup ordering),
            # fall back to the old "queue for dispatch" behavior so the
            # gate can re-verify state — this is strictly additive safety.
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
                # Single atomic UPDATE: mark failed AND set retry_count
                # to max_retries so the scanner treats the file as
                # permanently-blocked immediately.  Using the retry_count
                # override on update_status avoids the double-commit
                # race where a concurrent reader could see 'failed'
                # with retry_count still below max_retries.
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
                # Approval row gone or terminal non-approved state —
                # invalidate the inbox row and let the next scan
                # re-detect the file as new.
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
                # Still waiting — do nothing.  No dispatch, no Telegram,
                # no work.  The row stays in processing with its marker.
                logger.debug(
                    "Inbox row %s still awaiting approval %s",
                    row_id, request_id,
                )
                continue

            # approval_status == "approved" (or approval_manager is None,
            # fall-through "legacy" case): load content and queue for
            # dispatch.  The dispatcher will hit ensure_approval, find
            # the approved row, return cli_approved, and run CC.
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

        # Track resumed item IDs so the dispatch loop can skip the
        # pre-dispatch update_status(processing) for them.  That call
        # wipes error_message, which would clear the awaiting_approval
        # marker — if the process dies between the wipe and the blocked
        # branch re-setting the marker, the row would become invisible to
        # the resume pass AND stale enough to be evicted by
        # expire_stuck_processing.  Resumed items are already in
        # processing state with the correct marker; leave them alone.
        resumed_ids: set[str] = {item.id for item in resume_items}
        resumed_paths: set[str] = {item.file_path for item in resume_items}

        # Call-site gating pre-check: if an inbox_evaluation approval is
        # already pending, skip detection of new/modified files entirely.
        # No new inbox_items rows are created, no new approval requests
        # fire, and the next scan re-checks once the pending approval
        # resolves.  Resume items (already-pending rows) are still
        # dispatched below to handle state transitions.
        creation_blocked = False
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
                pending = None
            if pending is not None:
                logger.info(
                    "Inbox new/modified file detection skipped — "
                    "call site blocked on approval %s",
                    pending.get("id"),
                )
                creation_blocked = True

        # 2. Load known items from DB (skipped when creation is blocked:
        # detect_changes and the creation loops would just be no-ops).
        if creation_blocked:
            new_files: list[Path] = []
            modified_files: list[Path] = []
        else:
            known = await inbox_items.get_all_known(
                self._db, max_retries=self._config.max_retries,
            )

            # 3. Detect changes
            new_files, modified_files = detect_changes(
                watch, known, self._config.response_dir,
                recursive=self._config.recursive,
            )

        # Dedup resumed paths out of new/modified lists.  This closes a
        # narrow TOCTOU window: the resume pass computes compute_hash(p)
        # and matches it to the stored hash, then detect_changes computes
        # compute_hash(p) again.  If the file is edited between the two
        # calls, detect_changes may classify it as modified even though
        # the resume pass already queued it for dispatch.  Giving the
        # resume path priority avoids doubling up on the same file in a
        # single scan.
        if resumed_paths:
            new_files = [f for f in new_files if str(f) not in resumed_paths]
            modified_files = [
                f for f in modified_files if str(f) not in resumed_paths
            ]
        all_changed = new_files + modified_files

        if not all_changed and not resume_items:
            return CheckResult(
                items_found=len(scan_folder(
                    watch, self._config.response_dir,
                    recursive=self._config.recursive,
                )),
            )

        # 4. Create/update DB records for changed files.
        # NOTE: resume_items are kept in a SEPARATE list — they are
        # dispatched as forced singleton batches to preserve their
        # original content-stable approval key.  Only new/modified
        # items flow into pending_items for configured-batch-size
        # dispatch.
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
            # Retry storm prevention: stop re-evaluating files that
            # persistently fail URL fetches (e.g. permanent SSL issues).
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
            # Update existing record: create new entry with updated hash
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
            # Cooldown: skip if successfully evaluated recently
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
            # Look up previous evaluation for delta-only processing
            existing = await inbox_items.get_by_file_path(self._db, str(f))
            if existing and existing["status"] == "pending":
                await inbox_items.update_status(
                    self._db, existing["id"],
                    status="failed",
                    error_message="superseded_by_modification",
                )
            # Compute delta: only send content not previously evaluated
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

        # 4. Batch and dispatch
        batches_dispatched = 0
        batch_size = self._config.batch_size
        # Reflection MCP profile gives inbox sessions memory access
        # (genesis-health + genesis-memory) so evaluations can query
        # and store user signals via memory_recall / memory_store.
        mcp_path = SessionConfigBuilder().build_mcp_config("reflection")

        # Build the dispatch schedule: resume items ALWAYS go as singleton
        # batches (one item per dispatch call), new/modified items use the
        # configured batch_size.  Resume items are dispatched first so the
        # user sees pending-approval items resolved before new inbox work
        # queues behind them.
        scheduled_batches: list[list[InboxItem]] = [
            [item] for item in resume_items
        ]
        for i in range(0, len(pending_items), batch_size):
            scheduled_batches.append(pending_items[i : i + batch_size])

        for batch in scheduled_batches:
            batch_id = str(uuid.uuid4())

            # Update DB with batch_id and status=processing.
            # For resumed items, do NOT overwrite status/error_message —
            # they're already in processing with the awaiting_approval
            # marker, and clearing the marker here would leave a narrow
            # window where the row is invisible to the resume pass if the
            # process dies before the blocked branch rewrites it.
            for item in batch:
                await inbox_items.set_batch(self._db, item.id, batch_id=batch_id)
                if item.id not in resumed_ids:
                    await inbox_items.update_status(
                        self._db, item.id, status="processing",
                    )

            # Build prompt with all items
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

            # No allowed_tools restriction: --dangerously-skip-permissions
            # overrides --allowedTools (empirically verified 2026-03-17).
            # The PreToolUse hook in .claude/settings.json blocks dangerous
            # tool usage (e.g. WebFetch on YouTube URLs → redirects to yt-dlp).

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
                        # Inbox deliberately routes through the autonomous
                        # dispatcher for the approval gate only, never for
                        # API fallback.  Free-tier SLMs are not acceptable
                        # for user-facing inbox evaluation (quality > cost),
                        # but the manual-approval gate before firing a real
                        # CC session is the load-bearing piece of the April
                        # 4 autonomous-CLI hardening we want to preserve.
                        # Passing api_call_site_id=None makes route() skip
                        # the API attempt and go straight to the approval
                        # gate + CLI path.
                        api_call_site_id=None,
                        cli_fallback_allowed=True,
                        approval_required_for_cli=True,
                        # Do NOT include batch_id or item_count here.  The
                        # approval key is computed from this context, and
                        # batch_id is a fresh UUID per scan — including it
                        # would break dedup across scans and trigger a
                        # fresh Telegram prompt every 30 minutes for the
                        # same pending item.  The invocation.prompt
                        # already contains the full item content and is
                        # hashed into the approval key, so content-level
                        # uniqueness is preserved without the UUID noise.
                        context=None,
                    ),
                )
                if decision.mode == "blocked":
                    err = f"CLI fallback blocked: {decision.reason}"
                    # Distinguish "awaiting user approval" from other blocks
                    # (policy disabled, rejection).  If the user simply hasn't
                    # replied yet, keep the row alive in processing state with
                    # a marker so the next scan can resume it — no new row,
                    # no DB churn, no duplicate Telegram (the approval_key is
                    # stable now, so ensure_approval will find the existing
                    # pending request and skip the Telegram resend until the
                    # re-ask interval elapses).
                    #
                    # Detection logic: if the dispatcher returned an
                    # approval_request_id AND the reason does not indicate
                    # rejection, the approval is still pending.  The
                    # dispatcher emits "approval requested", "approval
                    # pending", or "approval pending; reminder sent" for
                    # pending cases and "existing rejection found" for
                    # rejection — we match on the rejection keyword because
                    # the pending phrases don't share a single substring.
                    reason_lower = decision.reason.lower()
                    is_pending_approval = (
                        decision.approval_request_id is not None
                        and "reject" not in reason_lower
                    )
                    if is_pending_approval:
                        # Not an error — this is the expected state while
                        # waiting for the user to approve in Telegram.  Log
                        # at INFO so parked items don't spam the dashboard
                        # with WARNING noise every 30 minutes.
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
                        # Don't append parked items to errors list — they're
                        # waiting, not failing.
                    else:
                        # Rejection, policy disabled, or other terminal block —
                        # mark failed so the standard retry/permanent-failure
                        # logic takes over.
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

            # Empty-output tripwire.  Even with api_call_site_id=None and
            # the AutonomousDispatchRouter empty-content fix upstream, a
            # CC session could theoretically return empty text (e.g.
            # upstream model timeout returning 200, transient edge cases).
            # Without this guard, the response writer concatenates
            # frontmatter + "" and produces a frontmatter-only file that
            # silently marks the item completed — the exact failure mode
            # that produced Genesis-4.genesis.md and
            # "My todos & musings.genesis.md" on 2026-04-06 and 2026-04-10.
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

            # Short-output warning: if the batch has URLs but the response
            # is suspiciously short, the model likely produced a full
            # evaluation as an intermediate text block that was overwritten
            # by a post-tool-call summary.  The CC CLI result field captures
            # only the final text block.  (Observed 2026-04-18: 14K eval
            # replaced by 735-char "Knowledge persisted" summary.)
            batch_content_for_urls = "\n".join(item.content for item in batch)
            urls_in_batch = len(_extract_urls(batch_content_for_urls))
            if urls_in_batch > 0 and output.text and len(output.text.strip()) < 1000:
                logger.warning(
                    "Inbox batch %s: response is only %d chars for %d URLs "
                    "— possible intermediate evaluation overwrite (CC result "
                    "captures only the final text block)",
                    batch_id[:8], len(output.text.strip()), urls_in_batch,
                )

            # Check if LLM classified as Acknowledged (context absorbed,
            # no response file needed).  Still store evaluated_content so
            # future delta computation works correctly.
            # Guard: only apply for single-item batches.  With batch_size>1
            # the response may contain both Acknowledged and non-Acknowledged
            # items — _is_acknowledged would false-positive for the whole batch.
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

                # Intentionally skip triage pipeline for Acknowledged items:
                # they are context/metadata, not findings worth triaging.
                batches_dispatched += 1
                continue

            # Write response file
            response_path = None
            if self._writer:
                try:
                    response_path = await self._writer.write_response(
                        batch_id=batch_id,
                        source_files=[item.file_path for item in batch],
                        evaluation_text=output.text,
                        item_count=len(batch),
                    )
                except Exception as exc:
                    err = f"Response write failed: {exc}"
                    errors.append(err)
                    logger.error(err)

            # Check for unresolved URL failures before marking complete.
            # If URLs failed, don't store evaluated_content (prevents delta
            # from masking failed URLs on future evaluations).
            batch_content = "\n".join(item.content for item in batch)
            url_failures = _has_url_failures(output.text, batch_content)
            if url_failures:
                logger.warning(
                    "URL failures detected in batch %s — marking as failed "
                    "to enable retry (response file still written for user)",
                    batch_id[:8],
                )

            completed_at = self._clock().isoformat()
            for item in batch:
                if url_failures:
                    # URL failures: preserve response but mark failed so
                    # the file is re-detected on the next cycle and
                    # evaluated with full content (no delta masking).
                    await inbox_items.mark_url_failure(
                        self._db, item.id,
                        response_path=str(response_path) if response_path else None,
                        processed_at=completed_at,
                    )
                else:
                    # Success: store full file content for future delta computation
                    try:
                        full_content = read_content(Path(item.file_path))
                    except (FileNotFoundError, PermissionError):
                        full_content = item.content
                    # Fallback: file may be transiently empty during Obsidian save
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

            # Complete CC session
            if used_cli and session_id is not None:
                await self._session_manager.complete(session_id)

            # Write message_queue entry for foreground context
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

            # Fire triage pipeline — same path as foreground conversations
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
