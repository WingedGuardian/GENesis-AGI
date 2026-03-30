"""CC Update Analyzer — fetch changelog, classify impact, alert if needed.

Triggered when CCVersionCollector detects a version change. Two paths:
1. Deep reflection context includes version_change observation, calls recon MCP tool
2. Recon MCP tool `recon_cc_update_check` triggers directly
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore
    from genesis.outreach.pipeline import OutreachPipeline
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

_GH_TIMEOUT = 15  # seconds
_CC_REPO = "anthropics/claude-code"

# Impact levels for CC updates
IMPACT_NONE = "none"
IMPACT_INFORMATIONAL = "informational"
IMPACT_ACTION_NEEDED = "action_needed"
IMPACT_BREAKING = "breaking"

_ANALYSIS_PROMPT = """\
Analyze this Claude Code version change for impact on Genesis (an autonomous AI agent
that uses Claude Code as its primary intelligence layer via background sessions).

Old version: {old_version}
New version: {new_version}

Changelog/release notes:
{changelog}

Genesis integration points to check for impact:
- CCInvoker flags: --bare, --model, --effort, --output-format, --mcp-config,
  --dangerously-skip-permissions, --allowedTools, --disallowedTools
- Hooks: PreToolUse, PostToolUse, SessionStart, Stop, UserPromptSubmit (settings.json)
- MCP servers: genesis-health, genesis-memory, genesis-recon, genesis-outreach
- Env vars: GENESIS_CC_SESSION, CLAUDE_STREAM_IDLE_TIMEOUT_MS
- Session management: --resume, -p (print mode), subprocess stdin prompt delivery
- Output parsing: JSON result type, stream-json events, cost/usage fields

Classify the impact as one of:
- none: No relevant changes for Genesis
- informational: Interesting but no action needed
- action_needed: Changes that may affect Genesis behavior (new flags, deprecated features, API changes)
- breaking: Changes that WILL break Genesis (removed flags, changed output format, session behavior changes)

Respond with ONLY a JSON object:
{{"impact": "<level>", "summary": "<1-2 sentence summary>", "details": "<relevant changes>"}}
"""


class CCUpdateAnalyzer:
    """Analyzes CC version changes for impact on Genesis."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        router: Router | None = None,
        pipeline: OutreachPipeline | None = None,
        memory_store: MemoryStore | None = None,
    ):
        self._db = db
        self._router = router
        self._pipeline = pipeline
        self._memory_store = memory_store

    async def analyze(self, old_version: str, new_version: str) -> dict:
        """Analyze a CC version change.

        Returns dict with: impact, summary, details, finding_id.
        """
        # 1. Fetch changelog
        changelog = await self._fetch_changelog(old_version, new_version)

        # 2. LLM analysis (if router available)
        if self._router and changelog:
            analysis = await self._llm_analyze(old_version, new_version, changelog)
        else:
            analysis = {
                "impact": IMPACT_INFORMATIONAL,
                "summary": f"CC updated from {old_version} to {new_version}",
                "details": changelog or "Changelog not available",
            }

        # 3. Store as recon finding (always include raw changelog)
        finding_id = await self._store_finding(old_version, new_version, analysis, changelog)
        analysis["finding_id"] = finding_id

        # 4. Alert if action_needed or breaking
        if analysis.get("impact") in (IMPACT_ACTION_NEEDED, IMPACT_BREAKING):
            await self._alert(analysis, old_version, new_version)

        return analysis

    @staticmethod
    def _version_to_tag(version: str) -> str:
        """Normalize version string to GitHub release tag.

        ``claude --version`` returns e.g. ``"2.1.84 (Claude Code)"``; GitHub
        release tags look like ``"v2.1.84"``.
        """
        bare = version.split("(")[0].strip()
        return f"v{bare}" if not bare.startswith("v") else bare

    async def _fetch_changelog(self, old_version: str, new_version: str) -> str:
        """Fetch release notes from GitHub via ``gh api``.

        Uses create_subprocess_exec (no shell) with a timeout. Falls back to
        empty string on any failure.
        """
        tag = self._version_to_tag(new_version)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "api",
                f"repos/{_CC_REPO}/releases",
                "--jq", ".[0:5]",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GH_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.warning(
                    "gh releases fetch failed (rc=%d): %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
                return ""

            releases = json.loads(stdout.decode("utf-8", errors="replace"))
            if not isinstance(releases, list):
                return ""

            for release in releases:
                if not isinstance(release, dict):
                    continue
                if release.get("tag_name") == tag:
                    body = release.get("body", "") or ""
                    if len(body) > 1000:
                        body = body[:1000] + "\n... (truncated)"
                    return body

            logger.debug("No release matching tag %s in latest 5 releases", tag)
            return ""
        except TimeoutError:
            logger.warning("gh releases fetch timed out")
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return ""
        except Exception:
            logger.debug("Failed to fetch changelog from GitHub", exc_info=True)
            return ""

    async def _llm_analyze(
        self, old_version: str, new_version: str, changelog: str,
    ) -> dict:
        """Use LLM to classify the update impact."""
        prompt = _ANALYSIS_PROMPT.format(
            old_version=old_version,
            new_version=new_version,
            changelog=changelog or "No changelog available",
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            result = await self._router.route_call("cc_update_analysis", messages)
            if result.success and result.content:
                return json.loads(result.content)
        except json.JSONDecodeError:
            logger.warning("LLM analysis returned non-JSON response", exc_info=True)
        except Exception:
            logger.warning("LLM analysis failed", exc_info=True)

        return self._fallback_analysis(old_version, new_version, changelog)

    @staticmethod
    def _fallback_analysis(old_version: str, new_version: str, changelog: str) -> dict:
        """Produce a structured fallback when LLM analysis is unavailable."""
        impact = IMPACT_INFORMATIONAL
        keywords_found: list[str] = []
        if changelog:
            lower = changelog.lower()
            checks = [
                ("hook", "hooks"),
                ("--bare", "CLI flags"),
                ("-p ", "print mode"),
                ("mcp", "MCP"),
                ("env var", "env vars"),
                ("breaking", "breaking changes"),
                ("removed", "removals"),
                ("security", "security"),
                ("fixed", "bug fixes"),
                ("permission", "permissions"),
                ("subprocess", "subprocess"),
            ]
            for pattern, label in checks:
                if pattern in lower:
                    keywords_found.append(label)
            if "breaking" in lower or "removed" in lower:
                impact = IMPACT_ACTION_NEEDED
        summary = f"CC updated {old_version} -> {new_version}"
        if keywords_found:
            summary += f" (areas: {', '.join(keywords_found[:5])})"
        else:
            summary += " (LLM analysis unavailable)"
        return {
            "impact": impact,
            "summary": summary,
            "details": changelog or "Changelog not available",
        }

    async def _store_finding(
        self, old_version: str, new_version: str, analysis: dict,
        changelog: str = "",
    ) -> str:
        """Store analysis as a recon finding in observations."""
        import uuid

        finding_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        impact = analysis.get("impact", IMPACT_INFORMATIONAL)
        summary = analysis.get("summary", "")

        finding_data: dict = {
            "old_version": old_version,
            "new_version": new_version,
            "impact": impact,
            "summary": summary,
            "details": analysis.get("details", ""),
            "analyzed_at": now,
        }
        if changelog:
            finding_data["changelog"] = changelog[:2000]
        content = json.dumps(finding_data)

        priority = "high" if impact in (IMPACT_ACTION_NEEDED, IMPACT_BREAKING) else "low"

        await self._db.execute(
            "INSERT INTO observations (id, source, type, category, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (finding_id, "recon", "finding", "cc_update", content, priority, now),
        )
        await self._db.commit()

        # Ingest to knowledge base (best-effort — observation is already stored)
        await self._ingest_to_knowledge(new_version, analysis, changelog)

        return finding_id

    async def _ingest_to_knowledge(
        self, new_version: str, analysis: dict, changelog: str,
    ) -> None:
        """Store CC update analysis in knowledge base for future retrieval."""
        if self._memory_store is None:
            return

        details = analysis.get("details", "")
        if not details:
            return

        summary = analysis.get("summary", f"CC update to {new_version}")
        impact = analysis.get("impact", IMPACT_INFORMATIONAL)
        tags = ["claude-code", "cc-update", impact]

        # Body: structured for retrieval — details first, raw changelog appended
        body = details
        if changelog and changelog not in details:
            body += f"\n\n## Raw changelog\n{changelog}"

        try:
            qdrant_id = await self._memory_store.store(
                body,
                f"cc_update:{new_version}",
                memory_type="knowledge",
                collection="knowledge_base",
                tags=tags,
                confidence=0.85,
                auto_link=False,
                source_pipeline="recon",
            )

            from genesis.db.crud import knowledge as knowledge_crud

            await knowledge_crud.insert(
                self._db,
                project_type="genesis-infra",
                domain="claude-code",
                source_doc=f"cc-release-{new_version}",
                concept=summary[:200],
                body=body,
                tags=json.dumps(tags),
                confidence=0.85,
                qdrant_id=qdrant_id,
                embedding_model=getattr(
                    self._memory_store._embeddings, "model_name", None,
                ),
            )
            logger.info("CC update %s ingested to knowledge base", new_version)
        except Exception:
            logger.warning(
                "Failed to ingest CC update %s to knowledge base",
                new_version, exc_info=True,
            )

    async def _alert(
        self, analysis: dict, old_version: str, new_version: str,
    ) -> None:
        """Send alert for action_needed or breaking changes via outreach."""
        impact = analysis.get("impact", "unknown")
        summary = analysis.get("summary", "CC update detected")
        details = analysis.get("details", "")

        logger.info("CC update alert [%s]: %s", impact, summary)

        if self._pipeline is None:
            logger.warning(
                "No outreach pipeline — CC update alert not delivered. "
                "This typically means the outreach subsystem failed to initialize "
                "or the pipeline_getter lambda resolved to None.",
            )
            return

        from genesis.outreach.types import OutreachCategory, OutreachRequest

        icon = {IMPACT_BREAKING: "\U0001f534", IMPACT_ACTION_NEEDED: "\U0001f7e1"}.get(
            impact, "\U0001f535",
        )
        lines = [
            f"{icon} CC VERSION UPDATE",
            "",
            f"{old_version} \u2192 {new_version}",
            f"Impact: {impact}",
            "",
            summary,
        ]
        if details:
            detail_text = details[:500] + ("..." if len(details) > 500 else "")
            lines.extend(["", "Key changes:", detail_text])

        text = "\n".join(lines)

        request = OutreachRequest(
            category=OutreachCategory.ALERT,
            topic=f"CC update {old_version} \u2192 {new_version}",
            context=text,
            salience_score=0.9,
            signal_type="cc_version_update",
        )

        try:
            result = await self._pipeline.submit(request)
            logger.info("CC update outreach: %s", result.status.value)
        except Exception:
            logger.error(
                "CC update outreach delivery failed for %s -> %s",
                old_version, new_version, exc_info=True,
            )
