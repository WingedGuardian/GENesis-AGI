"""CodeAuditExecutor — surplus executor with actual codebase context."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path

import aiosqlite

from genesis.routing.router import Router
from genesis.surplus.types import ExecutorResult, SurplusTask, TaskType

logger = logging.getLogger(__name__)

_IDENTITY_PATH = Path(__file__).parent.parent / "identity" / "CODE_AUDITOR.md"

# Prefixes that indicate generic/sycophantic audit output rather than
# actionable findings.  Checked against suggestion.strip().lower().
_SLOP_PREFIXES = (
    "great ",
    "excellent ",
    "the code looks",
    "the codebase looks",
    "the codebase is",
    "no issues",
    "no major issues",
    "no significant issues",
    "overall the codebase",
    "overall, the",
    "looks good",
    "well-structured",
    "well structured",
    "no findings",
)


def _is_slop(text: str) -> bool:
    """Return True if text is generic praise rather than an actionable finding."""
    lower = text.strip().lower()
    return any(lower.startswith(p) for p in _SLOP_PREFIXES)


def _safe_float(val: object, *, default: float = 0.5) -> float:
    """Convert to float, returning default on failure."""
    try:
        return float(val)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default
_MAX_CONTEXT_CHARS = 4000
_SUBPROCESS_TIMEOUT = 10


class CodebaseContextGatherer:
    """Assembles codebase context for code audit tasks via subprocess calls."""

    def __init__(self, db: aiosqlite.Connection, repo_root: str | None = None) -> None:
        self._db = db
        self._repo_root = repo_root or str(Path(__file__).parent.parent.parent.parent)

    async def _run_cmd(self, *args: str) -> str:
        """Run a subprocess command with timeout, return stdout or empty string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._repo_root,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_SUBPROCESS_TIMEOUT
            )
            return stdout.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            logger.warning("Subprocess timed out: %s", " ".join(args))
            return ""
        except OSError:
            logger.warning("Subprocess failed: %s", " ".join(args), exc_info=True)
            return ""

    async def _get_previous_findings(self) -> str:
        """Fetch unresolved code audit findings from observations table."""
        try:
            cursor = await self._db.execute(
                "SELECT content FROM observations "
                "WHERE source = 'recon' AND category = 'code_audit' AND resolved = 0 "
                "ORDER BY created_at DESC LIMIT 10"
            )
            rows = await cursor.fetchall()
            if not rows:
                return ""
            findings = [row[0] for row in rows]
            return "\n".join(f"- {f}" for f in findings)
        except Exception:
            logger.warning("Failed to fetch previous findings", exc_info=True)
            return ""

    async def gather(self) -> str:
        """Assemble codebase context, capped at ~4000 chars."""
        git_log, git_diff, file_tree, prev_findings = await asyncio.gather(
            self._run_cmd("git", "log", "--oneline", "-20"),
            self._run_cmd("git", "diff", "HEAD~5", "--stat"),
            self._run_cmd(
                "find", "src/genesis", "-name", "*.py", "-type", "f",
            ),
            self._get_previous_findings(),
        )

        # Truncate file tree to first 100 lines
        tree_lines = file_tree.splitlines()[:100]
        file_tree = "\n".join(tree_lines)

        sections = []
        sections.append("## Recent Commits\n" + (git_log or "(none)"))
        sections.append("## Recent Changes (stat)\n" + (git_diff or "(none)"))
        sections.append("## Source Files\n" + (file_tree or "(none)"))
        if prev_findings:
            sections.append("## Previous Unresolved Findings\n" + prev_findings)

        context = "\n\n".join(sections)
        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS] + "\n... (truncated)"
        return context


class CodeAuditExecutor:
    """Executes CODE_AUDIT surplus tasks with real codebase context."""

    def __init__(
        self,
        *,
        router: Router,
        db: aiosqlite.Connection,
        repo_root: str | None = None,
    ) -> None:
        self._router = router
        self._gatherer = CodebaseContextGatherer(db, repo_root=repo_root)

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        """Execute a code audit task. Conforms to SurplusExecutor protocol."""
        if task.task_type != TaskType.CODE_AUDIT:
            return ExecutorResult(
                success=False, error=f"Wrong task type: {task.task_type}"
            )

        # 1. Gather context
        try:
            context = await self._gatherer.gather()
        except Exception:
            logger.error("Failed to gather codebase context", exc_info=True)
            return ExecutorResult(success=False, error="context_gather_failed")

        # 2. Load identity
        identity = ""
        try:
            identity = _IDENTITY_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("CODE_AUDITOR.md not found at %s", _IDENTITY_PATH)
        except OSError:
            logger.warning("Failed to read CODE_AUDITOR.md", exc_info=True)

        # 3. Build messages and call router
        prompt = (
            "Analyze this codebase for issues. Return a JSON array of findings.\n"
            'Each finding: {"file": "...", "line": null, "severity": '
            '"low|medium|high", "suggestion": "...", "confidence": 0.0-1.0}\n\n'
            f"{context}"
        )

        messages: list[dict] = []
        if identity:
            messages.append({"role": "system", "content": identity})
        messages.append({"role": "user", "content": prompt})

        try:
            result = await self._router.route_call("36_code_auditor", messages)
        except Exception:
            logger.error("Router call failed for code audit", exc_info=True)
            return ExecutorResult(success=False, error="router_call_failed")

        if not result.success:
            return ExecutorResult(
                success=False, error=result.error or "routing_failed"
            )

        # 4. Parse findings
        insights = self._parse_findings(
            result.content or "",
            provider=result.provider_used or "unknown",
            drive_alignment=task.drive_alignment,
        )

        return ExecutorResult(
            success=True,
            content=result.content,
            insights=insights,
        )

    @staticmethod
    def _parse_findings(
        raw: str,
        *,
        provider: str,
        drive_alignment: str,
    ) -> list[dict]:
        """Parse JSON findings from LLM output, with fallback for malformed JSON."""
        text = raw.strip()

        # Try direct parse
        parsed = None
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            parsed = json.loads(text)

        # Try extracting from markdown code block
        if parsed is None:
            match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
            if match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    parsed = json.loads(match.group(1))

        # Try finding bare array
        if parsed is None:
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end > start:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    parsed = json.loads(text[start:end + 1])

        if not isinstance(parsed, list):
            # Fallback: return raw content as single insight with low confidence
            return [{
                "content": text[:500] if text else "No findings",
                "source_task_type": str(TaskType.CODE_AUDIT),
                "generating_model": provider,
                "drive_alignment": drive_alignment,
                "confidence": 0.15,
                "file": None,
                "line": None,
                "severity": "low",
                "suggestion": text[:500] if text else "No findings",
            }]

        insights = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            suggestion = item.get("suggestion", "")
            # Filter empty and generic/sycophantic suggestions
            if not suggestion or not suggestion.strip():
                logger.debug("Skipping code audit finding with empty suggestion")
                continue
            if _is_slop(suggestion):
                logger.debug("Skipping slop finding: %.80s", suggestion)
                continue
            insights.append({
                "content": item.get("suggestion", str(item)),
                "source_task_type": str(TaskType.CODE_AUDIT),
                "generating_model": provider,
                "drive_alignment": drive_alignment,
                "confidence": _safe_float(item.get("confidence", 0.5), default=0.5),
                "file": item.get("file"),
                "line": item.get("line"),
                "severity": item.get("severity", "low"),
                "suggestion": suggestion,
            })

        return insights if insights else [{
            "content": "No findings",
            "source_task_type": str(TaskType.CODE_AUDIT),
            "generating_model": provider,
            "drive_alignment": drive_alignment,
            "confidence": 0.15,
            "file": None,
            "line": None,
            "severity": "low",
            "suggestion": "No findings",
        }]
