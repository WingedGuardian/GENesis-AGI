"""MailMonitor — weekly batch orchestrator for Gmail email recon.

Two-layer architecture:
  Layer 1 (paralegal): Gemini Flash reads all emails, extracts findings,
    produces structured briefs, scores relevance. Low-signal emails filtered.
  Layer 2 (judge): CC Sonnet reviews surviving briefs, decides KEEP/DISCARD,
    produces refined findings for KEEP decisions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.db.crud import mail_items, observations
from genesis.mail.parser import parse_email
from genesis.mail.types import BatchResult, EmailBrief, MailConfig, ParsedEmail
from genesis.observability.types import Severity, Subsystem
from genesis.security.sanitizer import ContentSanitizer, ContentSource

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.invoker import CCInvoker
    from genesis.cc.session_manager import SessionManager
    from genesis.mail.imap_client import IMAPClient
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

_IDENTITY_DIR = Path(__file__).resolve().parents[1] / "identity"


class MailMonitor:
    """Weekly batch processor: fetch Gmail -> Gemini paralegal -> CC judge -> recon findings."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        config: MailConfig,
        imap_client: IMAPClient,
        router: Router,
        invoker: CCInvoker,
        session_manager: SessionManager,
        event_bus: GenesisEventBus | None = None,
        triage_pipeline=None,
        on_job_success=None,
        on_job_failure=None,
    ) -> None:
        self._db = db
        self._config = config
        self._imap = imap_client
        self._router = router
        self._invoker = invoker
        self._session_manager = session_manager
        self._event_bus = event_bus
        self._triage_pipeline = triage_pipeline
        self._on_job_success = on_job_success
        self._on_job_failure = on_job_failure
        self._scheduler = None

    async def start(self) -> None:
        """Register the weekly cron job."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        self._scheduler = AsyncIOScheduler()

        # Parse cron expression: "minute hour day_of_month month day_of_week"
        parts = self._config.cron_expression.split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        else:
            logger.warning(
                "Invalid cron expression %r, using default Sunday 5am",
                self._config.cron_expression,
            )
            trigger = CronTrigger(day_of_week="sun", hour=5)

        self._scheduler.add_job(
            self._run_batch_safe,
            trigger,
            id="mail_monitor_batch",
            max_instances=1,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        logger.info("Mail monitor scheduled: %s", self._config.cron_expression)

    async def stop(self) -> None:
        """Shut down the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Mail monitor stopped")

    async def _run_batch_safe(self) -> None:
        """Wrapper that catches all exceptions so the scheduler doesn't die."""
        try:
            result = await self.run_batch()
            logger.info(
                "Mail batch complete: fetched=%d, briefed=%d, "
                "low_signal=%d, kept=%d, discarded=%d, errors=%d",
                result.fetched,
                result.layer1_briefed,
                result.layer1_low_signal,
                result.layer2_kept,
                result.layer2_discarded,
                len(result.errors),
            )
            await self._emit_event(
                "batch.complete",
                f"Mail batch: fetched={result.fetched}, "
                f"kept={result.layer2_kept}, "
                f"discarded={result.layer2_discarded}, "
                f"low_signal={result.layer1_low_signal}",
                fetched=result.fetched,
                kept=result.layer2_kept,
                discarded=result.layer2_discarded,
                low_signal=result.layer1_low_signal,
            )
            if self._on_job_success:
                self._on_job_success("mail_monitor_batch")
        except Exception:
            logger.exception("Mail batch failed")
            await self._emit_event("batch.failed", "Mail batch failed unexpectedly")
            if self._on_job_failure:
                self._on_job_failure("mail_monitor_batch", "batch failed")

    async def run_batch(self) -> BatchResult:
        """Execute one full batch cycle. Can be called manually."""
        result = BatchResult()

        # 1. Fetch unread emails
        raw_emails = await self._imap.fetch_unread(
            max_count=self._config.max_emails_per_run,
        )
        result.fetched = len(raw_emails)
        if not raw_emails:
            await self._emit_heartbeat()
            return result

        # 2. Parse emails
        parsed: list[ParsedEmail] = []
        all_uids: list[int] = []
        for raw in raw_emails:
            all_uids.append(raw.uid)
            try:
                email = parse_email(raw.raw_bytes, uid=raw.uid)
                parsed.append(email)
            except Exception:
                logger.warning("Failed to parse email UID %d", raw.uid, exc_info=True)
                result.errors.append(f"Parse failed for UID {raw.uid}")

        # 3. Dedup against DB
        new_emails: list[ParsedEmail] = []
        for email in parsed:
            if await mail_items.exists_by_message_id(self._db, email.message_id):
                result.already_known += 1
            else:
                new_emails.append(email)

        if not new_emails:
            # Mark all as read even if all were known
            await self._imap.mark_read(all_uids)
            await self._emit_heartbeat()
            return result

        # 4. Record all new emails in DB as pending
        now = datetime.now(UTC).isoformat()
        email_ids: dict[str, str] = {}  # message_id -> db row id
        for email in new_emails:
            row_id = str(uuid.uuid4())
            content_hash = hashlib.sha256(email.message_id.encode()).hexdigest()[:16]
            await mail_items.create(
                self._db,
                id=row_id,
                message_id=email.message_id,
                imap_uid=email.imap_uid,
                sender=email.sender,
                subject=email.subject,
                received_at=email.date,
                body_preview=email.body[:500] if email.body else None,
                created_at=now,
                content_hash=content_hash,
            )
            email_ids[email.message_id] = row_id

        # 5. Layer 1: Gemini paralegal — analyze all emails, produce briefs
        try:
            relevant, low_signal = await self._layer1_analyze(
                new_emails, email_ids,
            )
            result.layer1_briefed = len(relevant)
            result.layer1_low_signal = len(low_signal)
        except Exception:
            logger.exception("Layer 1 analysis failed — sending all to judge")
            await self._emit_event(
                "layer1.failure",
                "Layer 1 analysis exception — all emails forwarded to judge",
            )
            relevant = _fallback_briefs(new_emails)
            low_signal = []
            result.layer1_briefed = len(relevant)

        # Mark low-signal emails in DB
        for brief in low_signal:
            msg_id = new_emails[brief.email_index - 1].message_id
            row_id = email_ids[msg_id]
            await mail_items.update_layer1_verdict(
                self._db, row_id, verdict="low_signal",
            )
            await mail_items.update_status(
                self._db, row_id, status="skipped", processed_at=now,
            )

        # 6. Layer 2: CC judge — review surviving briefs
        if relevant:
            for brief in relevant:
                msg_id = new_emails[brief.email_index - 1].message_id
                row_id = email_ids[msg_id]
                await mail_items.update_layer1_verdict(
                    self._db, row_id, verdict="relevant",
                )

            await self._layer2_judge(
                relevant, new_emails, email_ids, result,
            )

        # 7. Mark ALL fetched emails as read
        await self._imap.mark_read(all_uids)

        await self._emit_heartbeat()
        return result

    # ------------------------------------------------------------------
    # Layer 1: Gemini Flash paralegal
    # ------------------------------------------------------------------

    async def _layer1_analyze(
        self,
        emails: list[ParsedEmail],
        email_ids: dict[str, str],
    ) -> tuple[list[EmailBrief], list[EmailBrief]]:
        """Run Gemini Flash paralegal. Returns (relevant, low_signal)."""
        prompt = self._build_paralegal_prompt(emails)

        response = await self._router.route(prompt, purpose="outreach_email_triage")

        briefs = self._parse_briefs(response, len(emails))

        # Emit parse failure event if fallback briefs were generated
        has_parse_failures = any(
            "paralegal_parse_failure" in b.key_findings
            or "brief_parse_failure" in b.key_findings
            or "missing_from_paralegal_response" in b.key_findings
            for b in briefs
        )
        if has_parse_failures:
            await self._emit_event(
                "layer1.parse_failure",
                "Layer 1 paralegal returned partial or unparseable output "
                f"({len(emails)} emails, {len(briefs)} briefs)",
            )

        # Store all briefs in DB
        for brief in briefs:
            msg_id = emails[brief.email_index - 1].message_id
            row_id = email_ids[msg_id]
            await mail_items.update_layer1_brief(
                self._db, row_id, brief_json=json.dumps({
                    "email_index": brief.email_index,
                    "sender": brief.sender,
                    "subject": brief.subject,
                    "classification": brief.classification,
                    "relevance": brief.relevance,
                    "key_findings": brief.key_findings,
                    "assessment": brief.assessment,
                    "recommendation": brief.recommendation,
                }),
            )

        # Split by relevance threshold
        min_rel = self._config.min_relevance
        relevant = [b for b in briefs if b.relevance >= min_rel]
        low_signal = [b for b in briefs if b.relevance < min_rel]

        return relevant, low_signal

    def _parse_briefs(
        self, response: str, expected_count: int,
    ) -> list[EmailBrief]:
        """Parse Gemini's JSON response into EmailBrief objects.

        Per-brief parsing with field-level defaults. If the entire response
        is unparseable, returns fallback briefs (relevance=3) so all emails
        go to the judge.
        """
        try:
            json_str = response
            if "```" in response:
                start = response.find("[")
                end = response.rfind("]") + 1
                if start >= 0 and end > start:
                    json_str = response[start:end]

            raw_briefs = json.loads(json_str)
            if not isinstance(raw_briefs, list):
                raise TypeError(f"Expected list, got {type(raw_briefs).__name__}")
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.error(
                "Layer 1 paralegal returned unparseable response "
                "(len=%d, first 200 chars: %s)",
                len(response), response[:200],
                exc_info=True,
            )
            return _fallback_briefs_from_count(expected_count)

        # Truncate to expected count to prevent extra briefs
        raw_briefs = raw_briefs[:expected_count]

        seen_indices: set[int] = set()
        briefs: list[EmailBrief] = []
        for i, raw in enumerate(raw_briefs):
            try:
                relevance_val = raw.get("relevance", 3)
                if isinstance(relevance_val, str):
                    relevance_val = int(relevance_val)
                relevance_val = max(1, min(5, relevance_val))

                findings = raw.get("key_findings", [])
                if not isinstance(findings, list):
                    findings = [str(findings)] if findings else []

                raw_idx = int(raw.get("email_index", i + 1))
                clamped_idx = max(1, min(expected_count, raw_idx))

                # Deduplicate: if this index was already claimed, skip
                if clamped_idx in seen_indices:
                    continue
                seen_indices.add(clamped_idx)

                briefs.append(EmailBrief(
                    email_index=clamped_idx,
                    sender=str(raw.get("sender", "")),
                    subject=str(raw.get("subject", "")),
                    classification=str(raw.get("classification", "Unknown")),
                    relevance=relevance_val,
                    key_findings=[str(f) for f in findings],
                    assessment=str(raw.get("assessment", "")),
                    recommendation=str(raw.get("recommendation", "")),
                ))
            except (TypeError, ValueError):
                fallback_idx = max(1, min(expected_count, i + 1))
                if fallback_idx not in seen_indices:
                    seen_indices.add(fallback_idx)
                    logger.warning(
                        "Failed to parse brief %d, using fallback",
                        fallback_idx, exc_info=True,
                    )
                    briefs.append(EmailBrief(
                        email_index=fallback_idx,
                        sender="",
                        subject="",
                        classification="Unknown",
                        relevance=3,
                        key_findings=["brief_parse_failure"],
                        assessment="Paralegal output could not be parsed.",
                        recommendation="Judge should review the original email.",
                    ))

        # Backfill any missing indices with fallback briefs
        for idx in range(1, expected_count + 1):
            if idx not in seen_indices:
                briefs.append(EmailBrief(
                    email_index=idx,
                    sender="",
                    subject="",
                    classification="Unknown",
                    relevance=3,
                    key_findings=["missing_from_paralegal_response"],
                    assessment="Paralegal did not produce a brief for this email.",
                    recommendation="Judge should review the original email.",
                ))

        briefs.sort(key=lambda b: b.email_index)
        return briefs

    def _build_paralegal_prompt(self, emails: list[ParsedEmail]) -> str:
        """Build the Layer 1 paralegal prompt for Gemini."""
        parts = [
            "You are a paralegal for an AI research agent called Genesis. "
            "Your job is to read each email below thoroughly, extract the key "
            "findings, classify the content, and produce a structured brief.\n\n"
            "For each email, produce a JSON object with these fields:\n"
            "- email_index: (integer) the email number as listed below\n"
            "- sender: (string) who sent it\n"
            "- subject: (string) email subject line\n"
            "- classification: (string) one of: AI_Agent, Competitive, "
            "Research, Newsletter, Operational\n"
            "- relevance: (integer 1-5) how relevant this is to AI agent "
            "development\n"
            "  - 5: Critical — directly impacts our architecture or roadmap\n"
            "  - 4: High — significant development worth tracking closely\n"
            "  - 3: Moderate — interesting but not immediately actionable\n"
            "  - 2: Low — tangentially related, minor signal\n"
            "  - 1: Noise — not relevant to AI agent development\n"
            "- key_findings: (array of strings) the specific facts, "
            "announcements, or insights extracted from the email body\n"
            "- assessment: (string) your preliminary analysis of significance\n"
            "- recommendation: (string) what the reviewing judge should focus on\n\n"
            "IMPORTANT: Score relevance based on the actual EMAIL CONTENT, not "
            "just the subject line. A billing email that mentions a new product "
            "release in the footnotes should score based on that release, not "
            "on the billing.\n\n"
            "Respond with ONLY a JSON array. No markdown, no explanation, "
            "just the array.\n",
        ]

        sanitizer = ContentSanitizer()
        for i, email in enumerate(emails, 1):
            result = sanitizer.sanitize(email.body, ContentSource.EMAIL)
            parts.append(
                f"\n---\n\nEmail {i}:\n"
                f"  From: {email.sender}\n"
                f"  Subject: {email.subject}\n"
                f"  Date: {email.date}\n"
                f"  Body:\n{result.wrapped}\n",
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Layer 2: CC judge
    # ------------------------------------------------------------------

    async def _layer2_judge(
        self,
        briefs: list[EmailBrief],
        emails: list[ParsedEmail],
        email_ids: dict[str, str],
        result: BatchResult,
    ) -> None:
        """Run CC session to judge paralegal briefs. KEEP/DISCARD per email."""
        from genesis.cc.types import CCInvocation, background_session_dir

        system_prompt = self._load_system_prompt()
        prompt = self._build_judge_prompt(briefs)
        batch_id = str(uuid.uuid4())[:8]

        # Mark emails as processing
        for brief in briefs:
            msg_id = emails[brief.email_index - 1].message_id
            row_id = email_ids[msg_id]
            await mail_items.update_status(
                self._db, row_id, status="processing", batch_id=batch_id,
            )

        try:
            invocation = CCInvocation(
                prompt=prompt,
                model=self._config.model,
                effort=self._config.effort,
                system_prompt=system_prompt,
                timeout_s=self._config.timeout_s,
                skip_permissions=True,
                disallowed_tools=["Write", "Edit", "Agent", "NotebookEdit"],
                working_dir=background_session_dir(),
            )
            cc_output = await self._invoker.run(invocation)
            judge_text = cc_output.text

            # Parse judge decisions
            decisions = self._parse_judge_decisions(judge_text, briefs)

            # Process each decision
            now = datetime.now(UTC).isoformat()
            for decision in decisions:
                idx = decision["email_index"]
                msg_id = emails[idx - 1].message_id
                row_id = email_ids[msg_id]

                # Store judge decision in DB (audit trail for ALL judged emails)
                await mail_items.update_layer2_decision(
                    self._db, row_id, decision_json=json.dumps(decision),
                )

                if decision["decision"] == "KEEP":
                    await self._store_finding(
                        emails[idx - 1], decision, batch_id,
                    )
                    await mail_items.update_status(
                        self._db, row_id,
                        status="completed", processed_at=now,
                    )
                    result.layer2_kept += 1
                else:
                    await mail_items.update_status(
                        self._db, row_id,
                        status="skipped", processed_at=now,
                    )
                    result.layer2_discarded += 1

            # Fire triage pipeline for kept findings
            if self._triage_pipeline is not None and result.layer2_kept > 0:
                from genesis.cc.types import CCOutput
                from genesis.util.tasks import tracked_task

                kept_decisions = [
                    d for d in decisions if d["decision"] == "KEEP"
                ]
                findings_text = "\n\n".join(
                    f"## {d.get('refined_finding', d.get('rationale', ''))}"
                    for d in kept_decisions
                )
                user_text = "\n".join(
                    f"Subject: {emails[d['email_index'] - 1].subject}"
                    for d in kept_decisions
                )
                # Wrap findings in CCOutput so the triage pipeline can
                # access .session_id, .text, .input_tokens, etc.
                mail_output = CCOutput(
                    session_id=f"mail-batch-{datetime.now(UTC).strftime('%Y%m%dT%H%M')}",
                    text=findings_text,
                    model_used="mail-triage",
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    duration_ms=0,
                    exit_code=0,
                )
                tracked_task(
                    self._triage_pipeline(mail_output, user_text, "mail"),
                    name="mail-triage",
                    event_bus=self._event_bus,
                    subsystem=Subsystem.MAIL,
                )

        except Exception as exc:
            err = f"Layer 2 judge failed: {exc}"
            logger.error(err, exc_info=True)
            result.errors.append(err)
            result.layer2_failed = len(briefs)
            now = datetime.now(UTC).isoformat()
            for brief in briefs:
                msg_id = emails[brief.email_index - 1].message_id
                row_id = email_ids[msg_id]
                await mail_items.update_status(
                    self._db, row_id,
                    status="failed", error_message=err, processed_at=now,
                )

    def _parse_judge_decisions(
        self,
        judge_text: str,
        briefs: list[EmailBrief],
    ) -> list[dict]:
        """Parse judge output as JSON array of decisions.

        Each decision: {email_index, decision, rationale, refined_finding}.
        On parse failure, treats all as KEEP (generous fallback).
        """
        try:
            json_str = judge_text
            if "```" in judge_text:
                start = judge_text.find("[")
                end = judge_text.rfind("]") + 1
                if start >= 0 and end > start:
                    json_str = judge_text[start:end]

            raw_decisions = json.loads(json_str)
            if not isinstance(raw_decisions, list):
                raise TypeError(
                    f"Expected list, got {type(raw_decisions).__name__}",
                )

            # Build set of valid indices from briefs
            valid_indices = {b.email_index for b in briefs}

            decisions: list[dict] = []
            for i, raw in enumerate(raw_decisions):
                raw_idx = int(raw.get("email_index", 0))
                # Clamp to a valid brief index; fall back to i-th brief
                if raw_idx not in valid_indices:
                    raw_idx = briefs[min(i, len(briefs) - 1)].email_index

                decisions.append({
                    "email_index": raw_idx,
                    "decision": str(raw.get("decision", "KEEP")).upper(),
                    "rationale": str(raw.get("rationale", "")),
                    "refined_finding": str(raw.get("refined_finding", "")),
                })
            return decisions

        except (json.JSONDecodeError, TypeError, ValueError):
            logger.error(
                "Judge output unparseable — treating all as KEEP "
                "(len=%d, first 200 chars: %s)",
                len(judge_text), judge_text[:200],
                exc_info=True,
            )
            return [
                {
                    "email_index": b.email_index,
                    "decision": "KEEP",
                    "rationale": "Judge output could not be parsed; "
                    "defaulting to KEEP.",
                    "refined_finding": b.assessment,
                }
                for b in briefs
            ]

    async def _store_finding(
        self,
        email: ParsedEmail,
        decision: dict,
        batch_id: str,
    ) -> None:
        """Store a single KEEP decision as a recon observation."""
        now = datetime.now(UTC).isoformat()
        content_hash = hashlib.sha256(
            f"email_recon:{email.message_id}".encode(),
        ).hexdigest()[:16]

        if await observations.exists_by_hash(
            self._db, source="recon", content_hash=content_hash,
        ):
            return

        refined = decision.get("refined_finding", "")
        rationale = decision.get("rationale", "")
        content = (
            f"Email from {email.sender}: {email.subject}\n"
            f"Date: {email.date}\n"
            f"Batch: {batch_id}\n\n"
            f"{refined}\n\n"
            f"Judge rationale: {rationale}"
        )

        await observations.create(
            self._db,
            id=str(uuid.uuid4()),
            source="recon",
            type="finding",
            category="email_recon",
            content=content,
            priority="medium",
            created_at=now,
            content_hash=content_hash,
        )

    def _build_judge_prompt(self, briefs: list[EmailBrief]) -> str:
        """Build the Layer 2 judge prompt from paralegal briefs."""
        parts = [
            f"Review the following {len(briefs)} email brief(s) produced by "
            f"the paralegal. For each, decide KEEP or DISCARD.\n",
        ]

        for brief in briefs:
            findings_str = "\n".join(
                f"    - {f}" for f in brief.key_findings
            ) if brief.key_findings else "    (none extracted)"
            parts.append(
                f"\n---\n\n## Brief {brief.email_index}: {brief.subject}\n"
                f"**From:** {brief.sender}\n"
                f"**Classification:** {brief.classification}\n"
                f"**Relevance:** {brief.relevance}/5\n"
                f"**Key findings:**\n{findings_str}\n"
                f"**Assessment:** {brief.assessment}\n"
                f"**Recommendation:** {brief.recommendation}\n",
            )

        return "\n".join(parts)

    def _load_system_prompt(self) -> str:
        """Load the MAIL_EVALUATE.md system prompt."""
        path = _IDENTITY_DIR / "MAIL_EVALUATE.md"
        if not path.exists():
            logger.warning("MAIL_EVALUATE.md not found at %s", path)
            return "You are judging email briefs for an AI research agent."
        return path.read_text()

    # ------------------------------------------------------------------
    # Observability helpers
    # ------------------------------------------------------------------

    async def _emit_event(
        self, event_type: str, message: str, **details,
    ) -> None:
        """Emit an event to the event bus."""
        if self._event_bus:
            try:
                await self._event_bus.emit(
                    Subsystem.MAIL,
                    Severity.INFO,
                    event_type,
                    message,
                    **details,
                )
            except Exception:
                logger.warning(
                    "Failed to emit event %s", event_type, exc_info=True,
                )

    async def _emit_heartbeat(self) -> None:
        """Emit a heartbeat event."""
        if self._event_bus:
            try:
                await self._event_bus.emit(
                    Subsystem.MAIL,
                    Severity.DEBUG,
                    "heartbeat",
                    "Mail monitor heartbeat",
                )
            except Exception:
                logger.warning("Failed to emit heartbeat", exc_info=True)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _fallback_briefs(emails: list[ParsedEmail]) -> list[EmailBrief]:
    """Create pass-through briefs when Layer 1 fails entirely."""
    return [
        EmailBrief(
            email_index=i,
            sender=e.sender,
            subject=e.subject,
            classification="Unknown",
            relevance=3,
            key_findings=["layer1_failure_fallback"],
            assessment="Layer 1 analysis failed; forwarding to judge.",
            recommendation="Review original email content.",
        )
        for i, e in enumerate(emails, 1)
    ]


def _fallback_briefs_from_count(count: int) -> list[EmailBrief]:
    """Create minimal pass-through briefs when Gemini returns garbage."""
    return [
        EmailBrief(
            email_index=i,
            sender="",
            subject="",
            classification="Unknown",
            relevance=3,
            key_findings=["paralegal_parse_failure"],
            assessment="Paralegal output could not be parsed.",
            recommendation="Review original email content.",
        )
        for i in range(1, count + 1)
    ]
