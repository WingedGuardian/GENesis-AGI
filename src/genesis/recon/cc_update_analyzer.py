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
import re
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
_CHANGELOG_URL = (
    "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
)
# Multi-release ranges are analyzed map-reduce: the changelog is split into
# context-safe CHUNKS, each chunk is classified by one LLM call, and the
# per-chunk verdicts are aggregated (max severity). This never drops a release —
# a real CC bump spans ~20-35k tokens of changelog (measured), too large for one
# call, and any release in the range can carry a regression.
#
# _CHUNK_BUDGET is per-chunk chars and is sized to the SMALLEST-capacity provider
# in the `cc_update_analysis` chain so a chunk is servable by EVERY provider it
# could route to (primary, fallback, or offset-distributed — see _analyze_range).
# The binding limit is groq-free / gpt-oss-120b at 8000 TPM (config/
# model_profiles.yaml, free-text `limits:` — not machine-readable), and its
# reasoning tokens inflate TPM further; 16k chars ~= 4k input tokens leaves room
# for the ~600-token prompt + output + reasoning under 8000 TPM. If the chain's
# min-TPM provider changes, re-derive here. _MAX_CHUNKS bounds the LLM fan-out for
# a pathological range; when it bites, the newest chunks are kept and a LOUD
# marker names the omission (never a silent cut).
_CHUNK_BUDGET = 16000
_MAX_CHUNKS = 12

# Impact levels for CC updates
IMPACT_NONE = "none"
IMPACT_INFORMATIONAL = "informational"
IMPACT_ACTION_NEEDED = "action_needed"
IMPACT_BREAKING = "breaking"
# The closed set an LLM impact classification must belong to. Output outside this
# set is untrustworthy — the boundary normalizer (_coerce_analysis) rejects it to
# the keyword fallback rather than propagating an unvalidated severity.
_VALID_IMPACTS = frozenset(
    {IMPACT_NONE, IMPACT_INFORMATIONAL, IMPACT_ACTION_NEEDED, IMPACT_BREAKING}
)

_ANALYSIS_PROMPT = """\
Analyze this Claude Code version change for impact on Genesis (an autonomous AI
agent that uses Claude Code as its primary intelligence layer). Genesis runs on
a headless Linux server, often inside tmux, dispatching background CC sessions
via `claude -p` and using CC interactively for foreground development.

Old version: {old_version}
New version: {new_version}

Changelog/release notes:
{changelog}

Evaluate EVERY changelog entry through ALL of these lenses:

1. PROGRAMMATIC INTEGRATION — Does it affect how Genesis dispatches or consumes
   CC sessions? (CCInvoker flags: --bare, --model, --effort, --output-format,
   --mcp-config, --dangerously-skip-permissions, --allowedTools, --disallowedTools,
   -p mode, --resume, subprocess stdin/stdout, output parsing)

2. HOOK & PERMISSION PROTOCOL — Does it change how hooks work, what they can
   return, when they fire, or how permissions are evaluated? (PreToolUse,
   PostToolUse, SessionStart, Stop, UserPromptSubmit, PermissionDenied,
   --dangerously-skip-permissions, auto mode classifier)

3. MCP & TOOL ECOSYSTEM — Does it affect MCP server connections, tool
   availability, tool behavior, or the skill/plugin system?

4. INTERACTIVE CLI EXPERIENCE — Does it change terminal rendering, scrollback,
   display, keyboard shortcuts, UI layout, or any behavior the user sees in
   foreground sessions? Regressions here are high-impact even if programmatic
   integration is unaffected.

5. PERFORMANCE & STABILITY — Does it fix or introduce memory leaks, caching
   changes, startup time changes, context window management, or crash fixes?
   Does it affect long-running or resumed sessions?

6. SECURITY & TRUST MODEL — Does it change permission boundaries, sandbox
   behavior, credential handling, or trust assumptions?

7. PLATFORM & ENVIRONMENT — Does it have platform-specific behavior? Genesis
   runs on Linux headless (no desktop app), often in tmux. Flag anything that
   behaves differently on Linux/headless vs macOS/desktop.

8. MODEL & API — Does it change prompt caching, token counting, model
   selection, API request format, or cost/usage tracking?

Classify the OVERALL impact as one of:
- none: No relevant changes for Genesis
- informational: New optional capabilities that don't change existing behavior.
  Additive features, fixes to things we don't use. A new feature is
  informational even if valuable — "available" is not "required"
- action_needed: Changes to EXISTING behavior that could affect Genesis, OR
  regressions that degrade the user's experience. The test: "Could this change
  cause something that worked before to work differently or stop working?"
- breaking: Removal or incompatible change to a feature Genesis actively uses
  in production code today

Calibration: analyze each changelog entry independently. A new optional feature
is informational, not action_needed. A behavior change or regression that affects
the user's daily experience IS action_needed even if it doesn't touch the
programmatic integration surface. Report the highest-severity individual item
as the overall impact.

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
        """Analyze a CC version change across the WHOLE ``(old, new]`` range.

        Returns dict with: impact, summary, details, finding_id. A multi-release
        jump is analyzed map-reduce (chunk -> per-chunk LLM classification ->
        aggregate by max severity) so no intervening release is missed.
        """
        # 1. Fetch the full changelog range (all intervening releases).
        changelog = await self._fetch_changelog(old_version, new_version)

        # 2. Map-reduce LLM analysis (if router available).
        if self._router and changelog:
            analysis = await self._analyze_range(old_version, new_version, changelog)
        else:
            analysis = {
                "impact": IMPACT_INFORMATIONAL,
                "summary": f"CC updated from {old_version} to {new_version}",
                "details": changelog or "Changelog not available",
            }

        # 3. Store as recon finding. The observation/KB keep a bounded changelog
        #    preview (a full multi-release range can be ~100KB — the LLM-derived
        #    `details` is the durable value); the observation truncates further.
        stored_changelog = changelog[:8000] if changelog else ""
        finding_id = await self._store_finding(
            old_version, new_version, analysis, stored_changelog,
        )
        analysis["finding_id"] = finding_id

        # 4. Alert if action_needed or breaking.
        if analysis.get("impact") in (IMPACT_ACTION_NEEDED, IMPACT_BREAKING):
            await self._alert(analysis, old_version, new_version)

        return analysis

    async def _analyze_range(
        self, old_version: str, new_version: str, changelog: str,
    ) -> dict:
        """Map-reduce the changelog: classify each chunk, aggregate by severity.

        One LLM call per chunk (each within a safe context budget); the verdicts
        are reduced to a single ``{impact, summary, details}`` with the overall
        impact = highest severity across chunks. A single-chunk range (the common
        small bump) returns that chunk's analysis verbatim, preserving the prior
        single-call behaviour and contract.
        """
        chunks = self._chunk_changelog(changelog)
        if not chunks:
            return self._fallback_analysis(old_version, new_version, changelog)
        # Fan the chunk classifications out CONCURRENTLY, and DISTRIBUTE them across
        # the provider chain with a per-chunk ``chain_offset``. The router paces each
        # provider by its RPM via a rate gate, so gathering N calls that all enter at
        # the same first provider (mistral-large-free, 4 RPM = 15s apart) would
        # serialize them and blow the caller's deadline on the exact multi-release
        # jump this fix targets. ``chain_offset=i`` rotates chunk i to start at
        # chain[i % len(chain)] (independent rate gates), so up to len(chain) chunks
        # run truly concurrently. Mirrors the established pattern in
        # knowledge/distillation.py and eval/j9_batch.py. _llm_analyze never raises
        # (returns _fallback_analysis on any error), so no chunk can poison the gather.
        analyses = list(await asyncio.gather(*(
            self._llm_analyze(old_version, new_version, chunk, chain_offset=i)
            for i, chunk in enumerate(chunks)
        )))
        return self._aggregate_analyses(analyses, old_version, new_version)

    @classmethod
    def _aggregate_analyses(
        cls, analyses: list[dict], old_version: str, new_version: str,
    ) -> dict:
        """Reduce per-chunk analyses to one verdict. Pure.

        Overall impact = the highest severity seen in any chunk. Details are the
        per-chunk details, highest-severity first, each tagged with its impact so
        the aggregate stays traceable. A single analysis is returned unchanged.
        """
        if len(analyses) == 1:
            return dict(analyses[0])
        rank = {
            IMPACT_NONE: 0,
            IMPACT_INFORMATIONAL: 1,
            IMPACT_ACTION_NEEDED: 2,
            IMPACT_BREAKING: 3,
        }

        def _rank(a: dict) -> int:
            return rank.get(a.get("impact", IMPACT_INFORMATIONAL), 1)

        overall = max(analyses, key=_rank).get("impact", IMPACT_INFORMATIONAL)
        ordered = sorted(analyses, key=_rank, reverse=True)
        parts: list[str] = []
        for a in ordered:
            detail = (a.get("details") or "").strip()
            if detail:
                parts.append(f"[{a.get('impact', '?')}] {a.get('summary', '')}\n{detail}")
        details = "\n\n".join(parts)
        top = ordered[0]
        summary = (
            f"CC {old_version} -> {new_version}: analyzed across {len(analyses)} "
            f"changelog chunks; overall impact {overall}."
        )
        if top.get("summary"):
            summary += f" Highest-severity item: {top['summary']}"
        return {"impact": overall, "summary": summary, "details": details}

    @staticmethod
    def _version_to_tag(version: str) -> str:
        """Normalize version string to GitHub release tag.

        ``claude --version`` returns e.g. ``"2.1.84 (Claude Code)"``; GitHub
        release tags look like ``"v2.1.84"``.
        """
        bare = version.split("(")[0].strip()
        return f"v{bare}" if not bare.startswith("v") else bare

    async def _fetch_changelog(self, old_version: str, new_version: str) -> str:
        """Changelog covering ``(old_version, new_version]`` — the WHOLE range.

        Primary source is the repo's raw ``CHANGELOG.md``, from which every
        release section in the range is extracted, so a multi-release jump is
        analyzed against ALL intervening releases (not just the newest — the
        bug this replaces). Falls back to the GitHub releases API for the single
        ``new_version`` body, LOUDLY marked ``[PARTIAL]``, when the CHANGELOG
        cannot be fetched or yields no sections in range (downgrade, equal
        versions, or an unpublished target) — so a degraded analysis is visible
        rather than a silent regression to single-version behaviour. Returns
        "" only when both sources fail.
        """
        md = await self._fetch_changelog_md()
        if md:
            sections = self._extract_sections_in_range(md, old_version, new_version)
            if sections:
                return sections
            logger.info(
                "CHANGELOG.md fetched but no release sections in range %s..%s "
                "(downgrade, equal versions, or unpublished target) — "
                "using single-version fallback",
                old_version, new_version,
            )
        body = await self._fetch_release_body(new_version)
        if body:
            return (
                f"[PARTIAL] Full {old_version} -> {new_version} changelog range "
                f"was unavailable; showing only the {new_version} release "
                f"notes:\n\n{body}"
            )
        return ""

    async def _fetch_changelog_md(self) -> str:
        """Fetch the raw ``CHANGELOG.md``. Empty string on any failure.

        Uses ``curl`` via create_subprocess_exec (no shell) with a timeout;
        curl is more universally present than ``gh`` and needs no auth for a
        public raw file. Any failure degrades to the ``gh`` releases fallback.
        """
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-fsSL", _CHANGELOG_URL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GH_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.warning(
                    "CHANGELOG.md fetch failed (rc=%s): %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
                return ""
            return stdout.decode("utf-8", errors="replace")
        except TimeoutError:
            logger.warning("CHANGELOG.md fetch timed out")
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return ""
        except Exception:
            logger.debug("Failed to fetch CHANGELOG.md", exc_info=True)
            return ""

    async def _fetch_release_body(self, new_version: str) -> str:
        """Single-version release body from the GitHub releases API (fallback).

        The former primary path, retained as a loud fallback. Uses
        create_subprocess_exec (no shell) with a timeout. Returns "" when no
        release in the latest window matches ``new_version``.
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
                    return release.get("body", "") or ""

            logger.debug("No release matching tag %s in latest 5 releases", tag)
            return ""
        except TimeoutError:
            logger.warning("gh releases fetch timed out")
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return ""
        except Exception:
            logger.debug("Failed to fetch release body from GitHub", exc_info=True)
            return ""

    @staticmethod
    def _semver_tuple(version: str) -> tuple[int, ...]:
        """Numeric ``(major, minor, patch, ...)`` tuple from a version string.

        Strips a leading ``v`` and any ``" (Claude Code)"`` suffix. Returns
        ``()`` when no numeric version is present (sorts lowest).
        """
        m = re.search(r"\d+(?:\.\d+)*", version)
        return tuple(int(x) for x in m.group().split(".")) if m else ()

    @classmethod
    def _extract_sections_in_range(
        cls, changelog_md: str, old_version: str, new_version: str,
    ) -> str:
        """Concatenate EVERY ``## X.Y.Z`` section with ``old < version <= new``.

        Pure function (no I/O). Sections keep the CHANGELOG's own order (newest
        first). Only ``## X.Y.Z`` headers actually present are matched, so
        skipped version numbers are handled naturally. No size cap here — the
        full range is returned so the analyzer's map-reduce chunker
        (:meth:`_chunk_changelog`) can cover every release without dropping any.
        Returns "" when the range contains no sections (equal versions,
        downgrade, or unpublished target) — the caller then falls back to the
        single-version path.
        """
        old_t = cls._semver_tuple(old_version)
        new_t = cls._semver_tuple(new_version)
        header_re = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b.*$", re.MULTILINE)
        matches = list(header_re.finditer(changelog_md))
        selected: list[str] = []
        for i, m in enumerate(matches):
            ver_t = cls._semver_tuple(m.group(1))
            if not (old_t < ver_t <= new_t):
                continue
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog_md)
            selected.append(changelog_md[start:end].strip())
        return "\n\n".join(selected)

    @staticmethod
    def _length_split(text: str, budget: int) -> list[str]:
        """Split *text* into pieces of at most ``budget`` chars. Pure.

        Prefers to break on a newline in the last fifth of the window so a piece
        rarely cuts mid-line; falls back to a hard char cut when no boundary is
        available (a single line longer than ``budget``). This is the size-bound
        of last resort — it applies to any unit the section packer can't keep
        whole (an oversized release section, or the header-less ``[PARTIAL]``
        fallback body), so NO path escapes the budget.
        """
        text = text.strip()
        if len(text) <= budget:
            return [text] if text else []
        pieces: list[str] = []
        i, n = 0, len(text)
        while i < n:
            end = min(i + budget, n)
            if end < n:
                floor = i + (budget * 4 // 5)
                nl = text.rfind("\n", floor, end)
                if nl > i:
                    end = nl
            piece = text[i:end].strip()
            if piece:
                pieces.append(piece)
            i = end  # end > i always (budget >= 1), so this makes progress
        return pieces

    @classmethod
    def _chunk_changelog(
        cls, changelog: str, budget: int = _CHUNK_BUDGET, max_chunks: int = _MAX_CHUNKS,
    ) -> list[str]:
        """Split a concatenated changelog into context-safe chunks.

        Whole ``## X.Y.Z`` release sections are kept together where they fit — each
        chunk holds as many consecutive sections as fit ``budget`` chars. Any single
        unit larger than ``budget`` (an oversized release section, OR the header-less
        ``[PARTIAL]`` fallback body) is length-split by :meth:`_length_split`, so NO
        chunk ever exceeds the budget — the size bound holds on every path, not just
        the header path. Chunks keep the changelog's order (newest first). Capped at
        ``max_chunks``; when the cap bites, the newest chunks are kept and a LOUD
        marker names the dropped chunk count (never a silent cut).
        """
        if not changelog.strip():
            return []
        header_re = re.compile(r"^##\s+\d+\.\d+\.\d+\b", re.MULTILINE)
        starts = [m.start() for m in header_re.finditer(changelog)]
        if starts:
            raw_units = [
                changelog[s:(starts[i + 1] if i + 1 < len(starts) else len(changelog))].strip()
                for i, s in enumerate(starts)
            ]
        else:
            # No release headers (e.g. the [PARTIAL] single-version fallback body):
            # one unit, still length-bounded below.
            raw_units = [changelog.strip()]
        # Guarantee every unit fits the budget BEFORE packing (oversized section or
        # header-less body -> split by the same mechanism).
        units: list[str] = []
        for u in raw_units:
            units.extend(cls._length_split(u, budget) if len(u) > budget else [u])
        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for unit in units:
            if cur and cur_len + len(unit) > budget:
                chunks.append("\n\n".join(cur))
                cur, cur_len = [], 0
            cur.append(unit)
            cur_len += len(unit) + 2
        if cur:
            chunks.append("\n\n".join(cur))
        if len(chunks) > max_chunks:
            dropped = len(chunks) - max_chunks
            chunks = chunks[:max_chunks]
            marker = (
                f"\n\n[... {dropped} older changelog chunk(s) omitted — range too "
                f"large for a single analysis pass; review older releases manually]"
            )
            # Preserve the size-bound invariant: trim the last kept chunk so
            # chunk+marker still fits budget (it is already the OLDEST kept chunk,
            # so trimming its tail sheds the least-relevant content).
            if len(chunks[-1]) + len(marker) > budget:
                chunks[-1] = chunks[-1][: max(0, budget - len(marker))]
            chunks[-1] += marker
        return chunks

    @staticmethod
    def _coerce_analysis(parsed: object) -> dict | None:
        """Normalize an untrusted LLM analysis payload, or None if unusable.

        Returns ``{impact: <valid enum>, summary: str, details: str}`` when *parsed*
        is a dict carrying an impact in the closed enum set; returns None when the
        payload is not a dict OR the impact is outside the enum (the caller then
        falls back to keyword heuristics rather than propagating an unvalidated
        severity). Non-str ``summary``/``details`` are coerced to str — the reducer
        (``_aggregate_analyses``), knowledge-ingest (``_ingest_to_knowledge``), and
        ``_alert`` all index/``.strip()`` these fields, so a model that emits a
        number/list/object there must be sanitized at THIS single boundary, not
        crash three consumers downstream.
        """
        if not isinstance(parsed, dict):
            return None
        impact = parsed.get("impact")
        # isinstance guard FIRST: an unhashable impact (list/dict from a malformed
        # model) would raise TypeError on `in frozenset`; return None so the caller
        # takes the intended keyword-fallback branch instead of the generic except.
        if not isinstance(impact, str) or impact not in _VALID_IMPACTS:
            return None

        def _as_str(v: object) -> str:
            return v if isinstance(v, str) else ("" if v is None else str(v))

        return {
            "impact": impact,
            "summary": _as_str(parsed.get("summary")),
            "details": _as_str(parsed.get("details")),
        }

    async def _llm_analyze(
        self, old_version: str, new_version: str, changelog: str,
        *, chain_offset: int = 0,
    ) -> dict:
        """Use an LLM to classify the update impact.

        ``chain_offset`` rotates the provider chain so concurrent chunk calls (see
        :meth:`_analyze_range`) don't all serialize on the first provider's rate
        gate. The return is ALWAYS the normalized contract
        ``{impact: <enum>, summary: str, details: str}`` — untrusted LLM output is
        coerced/rejected at this single boundary (:meth:`_coerce_analysis`) so every
        downstream consumer is safe by construction.
        """
        prompt = _ANALYSIS_PROMPT.format(
            old_version=old_version,
            new_version=new_version,
            changelog=changelog or "No changelog available",
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            # cc_update_analysis — analyzes CC version changelogs for impact on
            # Genesis hooks/integrations. Non-numeric ID by intent.
            result = await self._router.route_call(
                "cc_update_analysis", messages, chain_offset=chain_offset,
            )
            if result.success and result.content:
                normalized = self._coerce_analysis(json.loads(result.content))
                if normalized is not None:
                    return normalized
                logger.warning(
                    "LLM analysis payload not usable (non-dict or invalid impact) "
                    "— using keyword fallback",
                )
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
            # Organized by evaluation lens — matches the LLM prompt structure
            checks = [
                # Lens 1: Programmatic integration
                ("--bare", "CLI flags"),
                ("-p ", "print mode"),
                ("--resume", "session resume"),
                ("output-format", "output format"),
                ("subprocess", "subprocess"),
                # Lens 2: Hooks & permissions
                ("hook", "hooks"),
                ("permission", "permissions"),
                ("auto mode", "auto mode"),
                # Lens 3: MCP & tools
                ("mcp", "MCP"),
                ("skill", "skills"),
                ("plugin", "plugins"),
                # Lens 4: Interactive CLI experience
                ("scrollback", "rendering"),
                ("alt-screen", "rendering"),
                ("flicker", "rendering"),
                ("terminal", "terminal"),
                ("keyboard", "keyboard"),
                # Lens 5: Performance & stability
                ("memory leak", "performance"),
                ("crash", "stability"),
                ("out-of-memory", "stability"),
                ("cache", "caching"),
                # Lens 6: Security
                ("security", "security"),
                ("credential", "credentials"),
                ("sandbox", "sandbox"),
                # Lens 7: Platform
                ("linux", "platform"),
                ("headless", "platform"),
                ("tmux", "platform"),
                # Lens 8: Model & API
                ("prompt cache", "prompt caching"),
                ("token", "tokens"),
                # General severity signals
                ("breaking", "breaking changes"),
                ("removed", "removals"),
                ("deprecated", "deprecations"),
                ("regression", "regression"),
                ("fixed", "bug fixes"),
                ("env var", "env vars"),
            ]
            for pattern, label in checks:
                if pattern in lower and label not in keywords_found:
                    keywords_found.append(label)
            # Phrase-level severity triggers — "removed" alone is too broad
            # (catches "Removed whitespace"). Use phrases that signal real removals.
            severity_phrases = (
                "breaking", "regression",
                "was removed", "has been removed", "been removed",
                "removed support", "removed flag", "no longer",
            )
            if any(phrase in lower for phrase in severity_phrases):
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

        from genesis.db.crud import observations

        await observations.create(
            self._db,
            id=finding_id,
            source="recon",
            type="finding",
            content=content,
            priority=priority,
            created_at=now,
            category="cc_update",
        )

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
            from genesis.memory.provenance import derive_origin_class

            # Use upsert so re-analysis of the same CC release version
            # replaces the stale row instead of failing on the
            # UNIQUE(project_type, domain, concept) constraint.  The concept
            # is derived from summary[:200] which is stable per-version.
            # WS-3: mirror the store() call's provenance — the ON CONFLICT
            # SET would otherwise regress a backfilled origin_class to NULL
            # on re-analysis.
            _uid, inserted = await knowledge_crud.upsert(
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
                source_pipeline="recon",
                origin_class=derive_origin_class(
                    source_pipeline="recon", collection="knowledge_base",
                ),
            )
            logger.info(
                "CC update %s %s knowledge base",
                new_version, "ingested to" if inserted else "refreshed in",
            )
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
            verbatim=True,  # composed changelog summary — never reword
        )

        try:
            result = await self._pipeline.submit(request)
            logger.info("CC update outreach: %s", result.status.value)
        except Exception:
            logger.error(
                "CC update outreach delivery failed for %s -> %s",
                old_version, new_version, exc_info=True,
            )
