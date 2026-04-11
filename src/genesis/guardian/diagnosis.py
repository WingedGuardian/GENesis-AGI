"""CC diagnosis engine — HOST-SIDE. Intelligent investigation and recovery via claude -p.

When confirmation reaches SURVEYING, invokes Claude Code on the host for
intelligent diagnosis AND recovery. CC has full tool access — it investigates
the problem, attempts recovery, and verifies the fix. It is the doctor, not
a report writer.

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
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from genesis.guardian.briefing import read_guardian_briefing
from genesis.guardian.collector import DiagnosticSnapshot
from genesis.guardian.config import GuardianConfig

logger = logging.getLogger(__name__)


class CCDiagnosisError(Exception):
    """CC diagnosis failed with a specific, capturable reason."""


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
    actions_taken: list[str] = field(default_factory=list)
    outcome: str = "escalate"  # "resolved" | "partially_resolved" | "escalate"
    reasoning: str = ""
    source: str = "cc"  # "cc" or "cc_unavailable"
    cc_failure_reason: str = ""  # Why CC was unavailable (empty = CC succeeded)


_FAILURE_INVENTORY = """
Known failure mode inventory (from real incidents):

| Mode | Signals | Root cause | Recovery |
|------|---------|------------|----------|
| OOM kill | Container running, services dead, journal "Killed process" | Memory exhaustion | RESTART_SERVICES or RESTART_CONTAINER |
| /tmp full | Services degraded, /tmp >95% | tmpfs overflow (512MB limit) | RESOURCE_CLEAR |
| Bridge crash loop | Health API down, NRestarts high | Code bug or dependency | RESTART_SERVICES, then REVERT_CODE |
| Bad deploy | Failure correlates with recent git commit | Code regression | REVERT_CODE |
| Container freeze | Ping OK, all APIs timeout, D-state processes | I/O deadlock | RESTART_CONTAINER |
| Qdrant down | Memory retrieval fails, Qdrant service inactive | Qdrant crash | RESTART_SERVICES |
| Full disk | Multiple services fail, disk >95% | Disk exhaustion | RESOURCE_CLEAR |
| Network partition | Container running, ping fails, APIs fail | Network issue | RESTART_CONTAINER |
| Total container death | All 5 probes fail | Catastrophic failure | RESTART_CONTAINER, then SNAPSHOT_ROLLBACK |
| killpg(1) | All processes dead simultaneously | Bad PGID in test/code | RESTART_CONTAINER |
| Systemd user manager death | All user services dead, systemd --user gone | OOM cascading | RESTART_CONTAINER |
| Page cache I/O storm | D-state processes, high io.pressure | Memory pressure cascade | RESTART_CONTAINER |
"""


def _build_diagnosis_prompt(
    diagnostic: DiagnosticSnapshot,
    signal_summary: str,
    container_name: str,
    briefing_context: str | None = None,
) -> str:
    """Build the full prompt for the CC diagnostic instance."""
    briefing_section = ""
    if briefing_context:
        briefing_section = f"""
## Genesis Context Briefing (from shared filesystem)

The following briefing was written by Genesis before it went down. It contains
service baselines, metric norms, and recent history that may help your diagnosis.

{briefing_context}
"""

    # Check Sentinel state from shared filesystem
    sentinel_section = ""
    try:
        import json as _json
        from pathlib import Path as _Path
        sentinel_last = _Path.home() / ".genesis" / "shared" / "sentinel" / "last_run.json"
        if sentinel_last.exists():
            sdata = _json.loads(sentinel_last.read_text())
            sentinel_section = f"""
## Sentinel Status (Container-Side Guardian)

Genesis has an internal health guardian called the Sentinel that runs inside
the container. Before you act, check what the Sentinel already tried:

Last Sentinel run: {sdata.get('timestamp', 'unknown')}
Trigger: {sdata.get('trigger_source', 'unknown')} — {sdata.get('diagnosis', 'no diagnosis')}
Actions taken: {', '.join(sdata.get('actions_taken', [])) or 'none'}
Resolved: {sdata.get('resolved', 'unknown')}

If the Sentinel already diagnosed and attempted to fix this problem, do NOT
repeat the same actions. Either try something different or escalate.
"""
    except Exception:
        logger.debug("Failed to read sentinel state for diagnosis prompt", exc_info=True)

    return f"""You are the Genesis Guardian — the system's last line of defense.
Genesis appears to be down. You are running on the host VM with full tool access.
Genesis runs inside the Incus container "{container_name}".

Your job: investigate the root cause, attempt recovery, and verify the fix worked.
You are the doctor — examine the patient, treat them, confirm the treatment worked.
{sentinel_section}

## Initial Diagnostic Data

{diagnostic.to_prompt_text()}

## Recent Signal History

{signal_summary}
{briefing_section}
{_FAILURE_INVENTORY}

## Available Commands

Run these via Bash:
- `incus exec {container_name} -- <cmd>` — Run a command inside the container
- `incus exec {container_name} -- su - ubuntu -c "<cmd>"` — Run as the ubuntu user
- `incus exec {container_name} -- su - ubuntu -c "systemctl --user restart genesis-bridge"` — Restart the main service
- `incus exec {container_name} -- su - ubuntu -c "systemctl --user status genesis-bridge"` — Check service status
- `incus exec {container_name} -- su - ubuntu -c "journalctl --user -n 200 --no-pager"` — Read recent logs
- `incus exec {container_name} -- su - ubuntu -c "cat /sys/fs/cgroup/memory.current"` — Check memory
- `incus exec {container_name} -- su - ubuntu -c "df -h"` — Check disk
- `incus exec {container_name} -- su - ubuntu -c "df -h /tmp"` — Check /tmp (512MB tmpfs)
- `incus exec {container_name} -- su - ubuntu -c "ps aux --sort=-%mem | head -20"` — Top processes
- `incus exec {container_name} -- su - ubuntu -c "cd ~/genesis && git log --oneline -5"` — Recent commits
- `incus exec {container_name} -- su - ubuntu -c "cd ~/genesis && git diff --stat"` — Uncommitted changes
- `incus info {container_name}` — Container status and resource usage
- `incus restart {container_name}` — Restart the entire container (last resort)
- `incus snapshot create {container_name} guardian-pre-recovery` — Snapshot BEFORE recovery

## Investigation Protocol

1. Start with the diagnostic data above — but DO NOT stop there. Investigate.
2. Read logs (`journalctl`), check processes (`ps aux`), inspect disk/memory.
3. Check `git log` — was there a recent code change that correlates with failure?
4. Form a hypothesis. State your confidence level.
5. If confidence >= 70%:
   a. Take an Incus snapshot first: `incus snapshot create {container_name} guardian-pre-recovery`
   b. Attempt recovery (least destructive first: restart service > restart container > rollback)
   c. Wait briefly, then verify: check health endpoint, service status, logs
6. If the fix didn't work, try a different approach.
7. If confidence < 70% or all approaches exhausted: ESCALATE to the user.

## Rules

- ALWAYS take an Incus snapshot before any destructive recovery action
- Prefer least destructive recovery: restart service > clear resources > restart container > rollback
- Never raise resource limits — fix root causes
- Never work around symptoms — diagnose the actual problem
- Check temporal patterns: what changed recently? What metric degraded first?
- If you can't determine the cause with >50% confidence after investigation, ESCALATE

## Final Report

When you've reached a conclusion (resolved or need to escalate), output your final
report as a JSON block:

```json
{{{{
  "likely_cause": "One-sentence root cause description",
  "confidence_pct": 85,
  "evidence": ["Evidence point 1", "Evidence point 2"],
  "recommended_action": "RESTART_SERVICES",
  "actions_taken": ["Took pre-recovery snapshot", "Restarted genesis-bridge", "Verified health endpoint responded"],
  "outcome": "resolved",
  "reasoning": "Multi-sentence explanation of your investigation and findings"
}}}}
```

Field values:
- `recommended_action`: RESTART_SERVICES | RESOURCE_CLEAR | REVERT_CODE | RESTART_CONTAINER | SNAPSHOT_ROLLBACK | ESCALATE
- `actions_taken`: what you actually did (investigation steps + recovery actions)
- `outcome`: "resolved" (you fixed it), "partially_resolved" (improved but not fully), or "escalate" (needs human)

Output this JSON block at the very end of your response."""


class DiagnosisEngine:
    """Diagnose and treat Genesis failures using CC as an agentic investigator.

    CC gets full tool access and runs as a multi-turn agent. It investigates
    the problem, attempts recovery, and verifies the fix. The timeout and
    max-turns are runaway guards — they should never fire during legitimate
    work.

    When CC is unavailable: ESCALATE without action (prime directive).
    """

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
        cc_failure_reason = ""

        if self._config.cc.enabled:
            try:
                return await self._diagnose_with_cc(diagnostic, signal_summary)
            except Exception as exc:
                cc_failure_reason = f"{type(exc).__name__}: {exc}"
                logger.error("CC diagnosis failed: %s", exc, exc_info=True)
        else:
            cc_failure_reason = "CC diagnosis disabled in Guardian config"

        return self._escalate_without_cc(
            diagnostic, cc_failure_reason=cc_failure_reason,
        )

    async def _diagnose_with_cc(
        self,
        diagnostic: DiagnosticSnapshot,
        signal_summary: str,
    ) -> DiagnosisResult | None:
        """Invoke claude -p for intelligent diagnosis and recovery.

        CC runs as a full agent with tool access. It investigates, attempts
        recovery, and verifies. The response contains a JSON report at the
        end summarizing what it found and did.
        """
        container_name = self._config.container_name

        # Read briefing from shared filesystem (if available)
        briefing_context = None
        if self._config.briefing.enabled:
            briefing_context = read_guardian_briefing(
                self._config.briefing_path,
                max_age_s=self._config.briefing.max_age_s,
            )

        prompt = _build_diagnosis_prompt(
            diagnostic, signal_summary, container_name, briefing_context,
        )
        cc_path = str(Path(self._config.cc.path).expanduser())
        work_dir = Path("~/.local/share/genesis-guardian").expanduser()
        work_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            cc_path, "-p",
            "--model", self._config.cc.model,
            "--output-format", "json",
            "--max-turns", str(self._config.cc.max_turns),
            "--dangerously-skip-permissions",
        ]

        logger.info(
            "Starting CC diagnosis: model=%s, max_turns=%d, timeout=%ds",
            self._config.cc.model, self._config.cc.max_turns,
            self._config.cc.timeout_s,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
        except TimeoutError as exc:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise CCDiagnosisError(
                f"CC timed out after {self._config.cc.timeout_s}s"
            ) from exc

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:300]
            logger.error(
                "CC diagnosis exited with code %d: %s",
                proc.returncode, stderr_text,
            )
            raise CCDiagnosisError(
                f"CC exited with code {proc.returncode}: {stderr_text}"
            )

        result = self._parse_cc_response(stdout.decode("utf-8", errors="replace"))
        if result is None:
            raise CCDiagnosisError(
                "No valid JSON diagnosis found in CC response"
            )
        return result

    def _parse_cc_response(self, raw: str) -> DiagnosisResult | None:
        """Parse the CC response into a DiagnosisResult.

        With multi-turn agentic CC, the response is wrapped in a JSON envelope
        (--output-format json). The ``result`` field contains the final text
        response, which should end with a JSON diagnosis block.
        """
        try:
            # Step 1: Unwrap CC's JSON envelope
            outer = json.loads(raw)
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

            # Step 2: Extract JSON from the response text
            data = self._extract_json_diagnosis(text)
            if data is None:
                logger.error("No valid JSON diagnosis found in CC response")
                logger.debug("CC response text (first 2000 chars): %s", text[:2000])
                return None

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
                actions_taken=data.get("actions_taken", []),
                outcome=data.get("outcome", "escalate"),
                reasoning=data.get("reasoning", ""),
                source="cc",
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error(
                "Failed to parse CC diagnosis response: %s", exc, exc_info=True,
            )
            logger.debug("Raw CC response: %s", raw[:2000])
            return None

    @staticmethod
    def _extract_json_diagnosis(text: str) -> dict | None:
        """Extract the JSON diagnosis block from CC's response text.

        Handles multiple formats:
        - Pure JSON (single-turn compat)
        - JSON in markdown fences
        - JSON block embedded in narrative text (multi-turn)
        """
        text = text.strip()

        # Try 1: Entire text is JSON
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "likely_cause" in data:
                return data
        except json.JSONDecodeError:
            pass

        # Try 2: JSON in markdown fences (```json ... ``` or ``` ... ```)
        # Search from end — the diagnosis block should be the last one
        fence_pattern = re.compile(
            r"```(?:json)?\s*\n(\{.*?\})\s*\n```",
            re.DOTALL,
        )
        for match in reversed(list(fence_pattern.finditer(text))):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and "likely_cause" in data:
                    return data
            except json.JSONDecodeError:
                continue

        # Try 3: Last JSON object in the text (no fences)
        # Match balanced braces — handles nested objects like evidence arrays
        brace_pattern = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
        for match in reversed(list(brace_pattern.finditer(text))):
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict) and "likely_cause" in data:
                    return data
            except json.JSONDecodeError:
                continue

        return None

    def _escalate_without_cc(
        self,
        diagnostic: DiagnosticSnapshot,
        *,
        cc_failure_reason: str = "",
    ) -> DiagnosisResult:
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
            actions_taken=[],
            outcome="escalate",
            reasoning=(
                f"CC diagnosis failed: {cc_failure_reason or 'unknown'}. "
                "Guardian collected diagnostic evidence (memory, disk, services, journal) "
                "but cannot safely determine root cause without CC cross-referencing signals. "
                f"ACTION REQUIRED: Check ~/.local/state/genesis-guardian/guardian.log, "
                f"then container logs via "
                f"'incus exec {self._config.container_name} -- "
                f"su - ubuntu -c \"journalctl --user -n 200\"'."
            ),
            source="cc_unavailable",
            cc_failure_reason=cc_failure_reason,
        )
