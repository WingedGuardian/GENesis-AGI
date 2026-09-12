"""Weekly models.md synthesis — updates the model catalog from recon findings.

Reads recent model intelligence findings from knowledge_units, builds a
prompt, and dispatches a CC background session to refresh the LOCAL models.md
overlay (``~/.genesis/output/models.md``). The overlay is seeded once from the
tracked reference doc (``docs/reference/models.md``) and thereafter updated in
place with NO git commit — so every clone's weekly run stops diverging tracked
source (which used to make every ``git pull`` conflict on models.md). The
tracked doc stays a maintainer-curated reference; ``scripts/promote_models_md.sh``
copies a reviewed overlay back onto it for a deliberate public refresh.

The overlay is a per-install FORK, not a mirror: it seeds once and later
upstream edits to the tracked doc do NOT re-seed it. Delete the overlay to
re-seed from the current tracked doc.

Scheduled via SurplusScheduler: Sunday 8am UTC, 2h after model intelligence.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.env import output_dir, repo_root

if TYPE_CHECKING:
    import aiosqlite

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

# Tracked reference doc (relative to repo root) — the one-time seed source for
# the local overlay; not written by this job.
_MODELS_MD_REL = "docs/reference/models.md"

_SESSION_PROMPT_TEMPLATE = """\
You are updating the Genesis model catalog.

## Task

Read the file `{models_md_path}`, apply the intelligence findings below,
and write the updated file back.

## Rules

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

## Validation (you MUST verify before writing)

- The output MUST contain these section headers:
  "## THE HEAVY LIFTERS"
  "## THE ALL-ROUNDERS"
  "## THE SPECIALISTS"
- Output length must be between 50% and 200% of the original file length.
- If no material changes are needed, update only the Last Reviewed date.

## Workflow

1. Read `{models_md_path}` with the Read tool
2. Apply the findings below to produce the updated content
3. Verify your output meets the validation rules above
4. Write the updated file with the Write tool

If the file is unchanged (no material findings to apply), skip steps 3-4
and report "no changes needed."

## Intelligence Findings (last 7 days)

{findings}
"""


class ModelsMdSynthesisJob:
    """Weekly job: synthesize recon findings into the local models.md overlay.

    Dispatches a CC background session to perform the update, rather than
    calling the LLM router directly.  This gives natural resilience (no
    single-provider dependency) and lets the session use the Write tool for
    file I/O.  The session writes the local overlay only — no git commit.
    """

    def __init__(self, *, db: aiosqlite.Connection):
        self._db = db

    async def run(self) -> dict:
        """Query findings and dispatch a CC session. Returns summary dict."""
        # 1. Query recent findings
        findings = await self._query_findings()
        if not findings:
            logger.info("Models.md synthesis: no actionable findings in last 7 days")
            return {"skipped": True, "reason": "no_findings"}

        # 2. Resolve the LOCAL overlay target, seeding it once from the tracked
        #    reference doc. The session edits the overlay in place (no git), so a
        #    clone's weekly run never diverges tracked source. Seed idiom mirrors
        #    scripts/setup-local-config.sh (copy shipped default if absent).
        serialized = self._serialize_findings(findings)
        overlay = output_dir() / "models.md"
        if not overlay.exists():
            tracked_seed = repo_root() / _MODELS_MD_REL
            if not tracked_seed.exists():
                logger.error("Models.md seed not found at %s", tracked_seed)
                return {"skipped": True, "reason": "no_seed"}
            overlay.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(tracked_seed, overlay)
            logger.info("Seeded models.md overlay from %s", tracked_seed)
        models_md_path = overlay

        prompt = _SESSION_PROMPT_TEMPLATE.format(
            models_md_path=str(models_md_path),
            findings=serialized,
        )

        # 3. Dispatch CC session
        from genesis.cc.direct_session import (
            CCModel,
            DirectSessionRequest,
            EffortLevel,
        )
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        runner = rt._direct_session_runner
        if runner is None:
            raise RuntimeError("DirectSessionRunner not available")

        request = DirectSessionRequest(
            prompt=prompt,
            profile="interact",
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            timeout_s=3600,
            notify=True,
            notify_on_failure_only=True,
            source_tag="models_md_synthesis",
            caller_context="schedule:models_md_synthesis",
            tool_exceptions=("Write",),
        )

        session_id = await runner.spawn(request)
        logger.info(
            "Models.md synthesis dispatched (%d findings, session=%s)",
            len(findings), session_id,
        )

        return {
            "dispatched": True,
            "session_id": session_id,
            "findings_count": len(findings),
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

    # Structural markers that the CC session must preserve.
    # Kept as class state for testability — the prompt embeds these rules.
    _REQUIRED_MARKERS = (
        "## THE HEAVY LIFTERS",
        "## THE ALL-ROUNDERS",
        "## THE SPECIALISTS",
    )

    @staticmethod
    def _validate_output(output: str, original: str) -> str | None:
        """Validate LLM output before writing. Returns error string or None.

        NOTE: In the CC-session model this validation is expressed as prompt
        instructions rather than code-enforced.  The method is retained for
        unit testing and as the canonical definition of the validation rules.
        """
        if not output:
            return "empty output"

        for marker in ModelsMdSynthesisJob._REQUIRED_MARKERS:
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
    def _serialize_findings(findings: list[dict]) -> str:
        """Render findings as compact text blocks for the session prompt."""
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
