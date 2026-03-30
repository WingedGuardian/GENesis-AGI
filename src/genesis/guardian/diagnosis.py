"""CC diagnosis engine — intelligent root cause analysis via claude -p.

When confirmation reaches SURVEYING, invokes Claude CLI on the host for
intelligent diagnosis.

PRIME DIRECTIVE: First, do no harm.

When CC is unavailable, the Guardian takes NO recovery actions. Any single
signal can lie — only CC can cross-reference multiple signals and exercise
judgment. Without CC, the Guardian is an alerting system: collect diagnostics,
report to the user, take no action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from genesis.guardian.collector import DiagnosticSnapshot
from genesis.guardian.config import GuardianConfig

logger = logging.getLogger(__name__)


class RecoveryAction(StrEnum):
    """Available recovery actions, in escalation order."""

    RESTART_SERVICES = "RESTART_SERVICES"
    RESOURCE_CLEAR = "RESOURCE_CLEAR"
    REVERT_CODE = "REVERT_CODE"
    RESTART_CONTAINER = "RESTART_CONTAINER"
    SNAPSHOT_ROLLBACK = "SNAPSHOT_ROLLBACK"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class DiagnosisResult:
    """Result from the diagnosis engine."""

    likely_cause: str
    confidence_pct: int
    evidence: list[str]
    recommended_action: RecoveryAction
    reasoning: str
    source: str  # "cc" or "cc_unavailable"


_FAILURE_INVENTORY = """
Known failure mode inventory:

| Mode | Signals | Root cause | Recovery |
|------|---------|------------|----------|
| OOM kill | Container running, services dead, journal "Killed process" | Memory exhaustion | RESTART_SERVICES or RESTART_CONTAINER |
| /tmp full | Services degraded, /tmp >95% | tmpfs overflow | RESOURCE_CLEAR |
| Bridge crash loop | Health API down, NRestarts high | Code bug or dependency | RESTART_SERVICES, then REVERT_CODE |
| Bad deploy | Failure correlates with recent git commit | Code regression | REVERT_CODE |
| Container freeze | Ping OK, all APIs timeout, D-state processes | I/O deadlock | RESTART_CONTAINER |
| Qdrant down | Memory retrieval fails, Qdrant service inactive | Qdrant crash | RESTART_SERVICES |
| Full disk | Multiple services fail, disk >95% | Disk exhaustion | RESOURCE_CLEAR |
| Network partition | Container running, ping fails, APIs fail | Network issue | RESTART_CONTAINER |
| Total container death | All 5 probes fail | Catastrophic failure | RESTART_CONTAINER, then SNAPSHOT_ROLLBACK |
"""


def _build_diagnosis_prompt(
    diagnostic: DiagnosticSnapshot,
    signal_summary: str,
) -> str:
    """Build the full prompt for the CC diagnostic instance."""
    return f"""You are Genesis's diagnostic brain. You are invoked ONLY when Genesis
appears to be down. Your job: analyze the system data, identify the root cause,
and recommend a specific recovery action.

## Diagnostic Data

{diagnostic.to_prompt_text()}

## Signal History

{signal_summary}

{_FAILURE_INVENTORY}

## Your Task

Analyze the diagnostic data and produce a JSON response with this exact schema:

```json
{{
  "likely_cause": "One-sentence description of the root cause",
  "confidence_pct": 85,
  "evidence": ["Evidence point 1", "Evidence point 2"],
  "recommended_action": "RESTART_SERVICES",
  "reasoning": "Multi-sentence explanation of your analysis"
}}
```

Rules:
- `recommended_action` MUST be one of: RESTART_SERVICES, RESOURCE_CLEAR,
  REVERT_CODE, RESTART_CONTAINER, SNAPSHOT_ROLLBACK, ESCALATE
- If confidence < 70%, set recommended_action to ESCALATE
- Never recommend raising resource limits
- Never recommend working around symptoms — fix the root cause
- Look at temporal patterns: what changed recently? what metric degraded first?
- Check the git state: was there a recent commit that could have caused this?

Respond with ONLY the JSON object, no markdown fences, no explanation outside JSON."""


class DiagnosisEngine:
    """Diagnose Genesis failures using CC. ESCALATE without action if CC unavailable."""

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config

    async def diagnose(
        self,
        diagnostic: DiagnosticSnapshot,
        signal_summary: str = "",
    ) -> DiagnosisResult:
        """Run diagnosis. Uses CC when available; ESCALATES without it.

        Prime directive: first, do no harm. Without CC's judgment to
        cross-reference signals, no programmatic recovery is safe.
        """
        if self._config.cc.enabled:
            try:
                result = await self._diagnose_with_cc(diagnostic, signal_summary)
                if result is not None:
                    return result
                logger.warning("CC diagnosis returned None — escalating without action")
            except Exception as exc:
                logger.error("CC diagnosis failed: %s", exc, exc_info=True)

        return self._escalate_without_cc(diagnostic)

    async def _diagnose_with_cc(
        self,
        diagnostic: DiagnosticSnapshot,
        signal_summary: str,
    ) -> DiagnosisResult | None:
        """Invoke claude -p for intelligent diagnosis."""
        prompt = _build_diagnosis_prompt(diagnostic, signal_summary)
        cc_path = str(Path(self._config.cc.path).expanduser())
        work_dir = Path("~/.local/share/genesis-guardian").expanduser()
        work_dir.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            cc_path, "-p",
            "--model", self._config.cc.model,
            "--output-format", "json",
            "--max-turns", "1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self._config.cc.timeout_s,
            )
        except TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.error("CC diagnosis timed out after %ds", self._config.cc.timeout_s)
            return None

        if proc.returncode != 0:
            logger.error(
                "CC diagnosis exited with code %d: %s",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:500],
            )
            return None

        return self._parse_cc_response(stdout.decode("utf-8", errors="replace"))

    def _parse_cc_response(self, raw: str) -> DiagnosisResult | None:
        """Parse the CC JSON response into a DiagnosisResult."""
        try:
            outer = json.loads(raw)
            # Extract text content from CC's JSON envelope
            if isinstance(outer, dict) and "result" in outer:
                text = outer["result"]
            elif isinstance(outer, dict) and "content" in outer:
                content = outer["content"]
                if isinstance(content, list):
                    text = "".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                else:
                    text = str(content)
            elif isinstance(outer, str):
                text = outer
            else:
                text = raw

            # Strip markdown fences if present
            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:],
                )

            data = json.loads(text)

            action_str = data.get("recommended_action", "ESCALATE")
            try:
                action = RecoveryAction(action_str)
            except ValueError:
                logger.warning("Unknown recovery action from CC: %s", action_str)
                action = RecoveryAction.ESCALATE

            return DiagnosisResult(
                likely_cause=data.get("likely_cause", "Unknown"),
                confidence_pct=int(data.get("confidence_pct", 0)),
                evidence=data.get("evidence", []),
                recommended_action=action,
                reasoning=data.get("reasoning", ""),
                source="cc",
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error(
                "Failed to parse CC diagnosis response: %s", exc, exc_info=True,
            )
            logger.debug("Raw CC response: %s", raw[:1000])
            return None

    def _escalate_without_cc(self, diagnostic: DiagnosticSnapshot) -> DiagnosisResult:
        """CC unavailable — collect evidence and ESCALATE. No recovery actions.

        Prime directive: first, do no harm. Any single signal can lie.
        Without CC to cross-reference signals and exercise judgment, no
        programmatic recovery is safe. Alert the user with everything
        we know, and let them decide.
        """
        evidence = []
        evidence.append(f"Container: {diagnostic.container_status}")

        if diagnostic.memory.current_bytes > 0:
            current_mb = diagnostic.memory.current_bytes / (1024 * 1024)
            max_mb = diagnostic.memory.max_bytes / (1024 * 1024)
            evidence.append(
                f"Memory: {current_mb:.0f}MB / {max_mb:.0f}MB "
                f"({diagnostic.memory.usage_pct:.0f}%)",
            )

        for disk in diagnostic.disks:
            evidence.append(f"Disk {disk.mount}: {disk.usage_pct:.0f}% used")

        for svc in diagnostic.services:
            evidence.append(f"Service {svc.name}: {'active' if svc.active else 'dead'}")

        if "killed process" in diagnostic.journal_recent.lower():
            evidence.append("OOM kill signature found in journal")

        return DiagnosisResult(
            likely_cause="CC diagnosis unavailable — cannot determine root cause safely",
            confidence_pct=0,
            evidence=evidence,
            recommended_action=RecoveryAction.ESCALATE,
            reasoning="Guardian's diagnostic brain (CC) is unavailable. "
                      "Without intelligent cross-referencing of signals, any "
                      "programmatic recovery risks acting on a false signal. "
                      "Escalating to user with full diagnostic dump. "
                      "Manual investigation required.",
            source="cc_unavailable",
        )
