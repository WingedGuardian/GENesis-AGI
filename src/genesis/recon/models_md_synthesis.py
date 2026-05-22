"""Weekly models.md synthesis — updates the model catalog from recon findings.

Reads recent model intelligence findings from knowledge_units, feeds them
alongside the current docs/reference/models.md to Sonnet, and writes
back the updated file with a git commit.

Scheduled via SurplusScheduler: Sunday 8am UTC, 2h after model intelligence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.env import repo_root

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Finding types that are actionable for models.md updates.
# Excludes 'new_model' (bulk catalog of ALL 100k+ context models)
# and 'benchmark_unmatched' (unknown models with benchmark data).
_ACTIONABLE_TYPES = frozenset({
    "pricing_change",
    "benchmark_enrichment",
    "new_free_model",
    "free_model_removed",
    "free_model_inventory",
    "stale_profile",
})

# Structural markers that must be present in the output to pass validation.
_REQUIRED_MARKERS = (
    "## THE HEAVY LIFTERS",
    "## THE ALL-ROUNDERS",
    "## THE SPECIALISTS",
)

_SYSTEM_PROMPT = """\
You maintain a model catalog (models.md) for an AI agent system called Genesis.
You receive the current file and recent model intelligence findings. Your job:

1. UPDATE structured fields when findings provide new data:
   - Pricing (the **Cost:** line)
   - Benchmark scores (SWE-Bench fields, benchmark comparison table)
   - Free tier status and terms
   - Context window sizes
2. ADD genuinely notable NEW models to the "Pending Evaluation" section ONLY.
   Do NOT promote new models into a capability tier. Most findings are from a
   broad automated scan — only add models that fill a real gap in the current
   roster or represent a notable new capability from a major provider.
3. APPEND a dated changelog entry to the "Last Reviewed" section at the bottom
   summarizing what was updated. Use the format: **YYYY-MM-DD** — description.
4. PRESERVE all existing editorial voice, structure, and commentary. Do NOT
   rewrite Best At / Worst For bullets, Selection Cheat Sheet entries, or
   Genesis-specific notes. Do NOT reorder sections.
5. Do NOT change: Effort Level Assignments, routing recommendations, the HTML
   comment header at the top of the file.
6. Return the COMPLETE updated file contents. Do not truncate or summarize.
   If nothing material changed, return the file unchanged except for updating
   the Last Reviewed date.
"""


class ModelsMdSynthesisJob:
    """Weekly job: synthesize recon findings into docs/reference/models.md."""

    def __init__(self, *, db: aiosqlite.Connection, router: Router):
        self._db = db
        self._router = router

    async def run(self) -> dict:
        """Run the synthesis. Returns a summary dict."""
        # 1. Query recent findings
        findings = await self._query_findings()
        if not findings:
            logger.info("Models.md synthesis: no actionable findings in last 7 days")
            return {"skipped": True, "reason": "no_findings"}

        # 2. Read current models.md
        models_md_path = repo_root() / "docs" / "reference" / "models.md"
        if not models_md_path.exists():
            logger.error("Models.md not found at %s", models_md_path)
            return {"skipped": True, "reason": "file_not_found"}
        current_content = models_md_path.read_text(encoding="utf-8")

        # 3. Build LLM prompt
        serialized = self._serialize_findings(findings)
        user_prompt = (
            f"## Recent Intelligence Findings (last 7 days)\n\n"
            f"{serialized}\n\n"
            f"## Current models.md\n\n"
            f"{current_content}"
        )

        # 4. Call Sonnet via router
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        result = await self._router.route_call(
            "models_md_synthesis", messages, max_tokens=16384,
        )
        if not result.success:
            logger.error(
                "Models.md synthesis LLM call failed: %s", result.error,
            )
            raise RuntimeError(f"LLM call failed: {result.error}")

        updated_content = (result.content or "").strip()

        # 5. Validate output
        validation_error = self._validate_output(updated_content, current_content)
        if validation_error:
            logger.warning(
                "Models.md synthesis validation failed: %s", validation_error,
            )
            return {"skipped": True, "reason": f"validation_failed: {validation_error}"}

        # 6. Check for meaningful changes
        if updated_content.strip() == current_content.strip():
            logger.info("Models.md synthesis: no changes detected")
            return {"skipped": True, "reason": "no_changes"}

        # 7. Write file
        models_md_path.write_text(updated_content + "\n", encoding="utf-8")
        logger.info("Models.md updated (%d -> %d bytes)", len(current_content), len(updated_content))

        # 8. Git commit
        committed = await self._git_commit(models_md_path)

        return {
            "findings_count": len(findings),
            "updated": True,
            "committed": committed,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
        }

    async def _query_findings(self) -> list[dict]:
        """Query actionable findings from knowledge_units."""
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        cursor = await self._db.execute(
            """
            SELECT concept, body, ingested_at
            FROM knowledge_units
            WHERE domain = 'intelligence.models'
              AND ingested_at > ?
            ORDER BY ingested_at DESC
            LIMIT 200
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()

        findings = []
        for _concept, body, _ingested_at in rows:
            parsed = self._parse_body(body)
            if parsed and parsed.get("type") in _ACTIONABLE_TYPES:
                findings.append(parsed)
        return findings

    @staticmethod
    def _parse_body(body: str) -> dict | None:
        """Extract JSON from finding body (prefix + JSON format)."""
        match = re.search(r"\{.*\}", body, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _serialize_findings(findings: list[dict]) -> str:
        """Render findings as compact text blocks for the LLM prompt."""
        parts = []
        for f in findings:
            ftype = f.get("type", "unknown")
            title = f.get("title", ftype)
            # Remove the "Model intelligence: " prefix for readability
            title = title.replace("Model intelligence: ", "")
            details = json.dumps(
                {k: v for k, v in f.items() if k not in ("title", "type")},
                indent=2,
                default=str,
            )
            parts.append(f"### {title} ({ftype})\n```json\n{details}\n```")
        return "\n\n".join(parts)

    @staticmethod
    def _validate_output(output: str, original: str) -> str | None:
        """Validate LLM output before writing. Returns error string or None."""
        if not output:
            return "empty output"

        for marker in _REQUIRED_MARKERS:
            if marker not in output:
                return f"missing structural marker: {marker}"

        orig_len = len(original)
        out_len = len(output)
        if out_len < orig_len * 0.5:
            return f"too short ({out_len} vs {orig_len} original, <50%)"
        if out_len > orig_len * 2.0:
            return f"too long ({out_len} vs {orig_len} original, >200%)"

        return None

    @staticmethod
    async def _git_commit(file_path: Path) -> bool:
        """Stage and commit the updated file. Returns True if committed."""
        repo = repo_root()
        try:
            # git add — uses create_subprocess_exec (no shell injection risk)
            proc = await asyncio.create_subprocess_exec(
                "git", "add", str(file_path.relative_to(repo)),
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

            # git commit
            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m",
                "docs(models): weekly synthesis update\n\n"
                "Auto-generated from model intelligence recon findings.",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                msg = stderr.decode(errors="replace").strip()
                if "nothing to commit" in msg:
                    logger.info("Models.md commit: no changes to commit")
                else:
                    logger.warning("Models.md commit failed: %s", msg)
                return False
            logger.info("Models.md committed to git")
            return True
        except Exception:
            logger.exception("Failed to git commit models.md update")
            return False
