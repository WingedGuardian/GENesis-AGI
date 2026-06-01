"""Post-execution auditor — verifies what a dispatched session actually did.

Parses the CC transcript (.jsonl) to extract file paths touched by Write/Edit
tool calls, cross-references against ProtectedPathRegistry, and feeds
success/correction signals back to the AutonomyManager.

All audit data is tagged with source_subsystem="autonomy" so it never
surfaces to the ego via memory recall (contamination prevention).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    """Outcome of auditing a completed session."""

    success: bool
    files_touched: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    error: str | None = None


class PostExecutionAuditor:
    """Audits completed background sessions and feeds autonomy signals.

    Lean scope (V1): protected path violations + success/failure tracking.
    Future: expected_outputs comparison, tool anomaly detection.
    """

    def __init__(
        self,
        *,
        protected_paths: object | None = None,
        autonomy_manager: object | None = None,
        event_bus: object | None = None,
    ) -> None:
        self._protected_paths = protected_paths
        self._autonomy_manager = autonomy_manager
        self._event_bus = event_bus

    async def audit_session(
        self,
        session_id: str,
        *,
        transcript_path: str = "",
        tools_summary: dict | None = None,
        session_success: bool = True,
        caller_context: str = "",
    ) -> AuditResult:
        """Audit a completed session and feed signals to AutonomyManager.

        Parameters
        ----------
        session_id:
            Genesis session ID.
        transcript_path:
            Path to the CC transcript .jsonl file.
        tools_summary:
            Aggregated {tool_name: count} dict (for quick pre-filter).
        session_success:
            Whether the session completed without error.
        caller_context:
            E.g. "ego_proposal:abc123" — identifies what triggered this session.
        """
        # Quick pre-filter: if no Write/Edit in tools_summary, skip parsing
        if tools_summary and not any(
            t in tools_summary for t in ("Write", "Edit")
        ):
            # No file-modifying tools called — record success and return
            if session_success:
                await self._record_success()
            else:
                await self._record_correction("session_error")
            return AuditResult(success=session_success)

        # Parse transcript for file paths
        files_touched: list[str] = []
        if transcript_path:
            files_touched = self._parse_transcript(transcript_path)

        # Cross-reference against protected paths
        violations = self._check_protected_paths(files_touched)

        # Determine audit outcome
        if violations:
            await self._record_correction("protected_path_violation")
            await self._emit_event(
                "autonomy.audit.violation",
                session_id=session_id,
                violations=violations,
                files_touched=files_touched,
                caller_context=caller_context,
            )
            return AuditResult(
                success=False,
                files_touched=files_touched,
                violations=violations,
            )

        if not session_success:
            await self._record_correction("session_error")
            return AuditResult(success=False, files_touched=files_touched)

        # Clean pass
        await self._record_success()
        await self._emit_event(
            "autonomy.audit.pass",
            session_id=session_id,
            files_touched=files_touched,
            caller_context=caller_context,
        )
        return AuditResult(success=True, files_touched=files_touched)

    def _parse_transcript(self, path: str) -> list[str]:
        """Extract file paths from Write/Edit tool calls in .jsonl transcript."""
        transcript = Path(path)
        if not transcript.exists():
            logger.warning("Transcript not found: %s", path)
            return []

        files: list[str] = []
        try:
            for line in transcript.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue

                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in ("Write", "Edit"):
                        continue
                    fp = block.get("input", {}).get("file_path", "")
                    if fp:
                        files.append(fp)
        except Exception:
            logger.error("Failed to parse transcript: %s", path, exc_info=True)

        return files

    def _check_protected_paths(self, files: list[str]) -> list[str]:
        """Check file paths against ProtectedPathRegistry."""
        if not self._protected_paths or not files:
            return []

        classify = getattr(self._protected_paths, "classify", None)
        if classify is None:
            return []

        from genesis.autonomy.types import ProtectionLevel

        violations = []
        for fp in files:
            try:
                level = classify(fp)
                if level in (ProtectionLevel.CRITICAL, ProtectionLevel.SENSITIVE):
                    violations.append(f"{level.value}: {fp}")
            except Exception:
                continue

        return violations

    async def _record_success(self) -> None:
        """Feed success signal to AutonomyManager."""
        if self._autonomy_manager is None:
            return
        try:
            await self._autonomy_manager.record_success("background_cognitive")
        except Exception:
            logger.debug("Autonomy success signal failed", exc_info=True)

    async def _record_correction(self, reason: str) -> None:
        """Feed correction signal to AutonomyManager."""
        if self._autonomy_manager is None:
            return
        try:
            from datetime import UTC, datetime
            await self._autonomy_manager.record_correction(
                "background_cognitive",
                corrected_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            logger.debug("Autonomy correction signal failed", exc_info=True)

    async def _emit_event(self, event_type: str, **kwargs: object) -> None:
        """Emit an autonomy audit event (ego-invisible)."""
        if self._event_bus is None:
            return
        try:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                event_type,
                kwargs.get("caller_context", ""),
                **{k: v for k, v in kwargs.items() if k != "caller_context"},
            )
        except Exception:
            logger.debug("Audit event emission failed", exc_info=True)
