#!/usr/bin/env python3
"""sweep_fetch.py — Structured job-description sweep from a research spec.

Reads a career-ops research spec YAML, discovers job listings through
ATS APIs and web search, fetches individual JD pages, deduplicates with
fuzzy matching, extracts structured fields via LLM, and writes output.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/sweep_fetch.py path/to/spec.yml [--dry-run] [--verbose] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx
import yaml

# ── Genesis path injection (standard script pattern) ──────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Optional imports with graceful degradation ────────────────────────
try:
    import litellm

    litellm.suppress_debug_info = True
    _HAS_LITELLM = True
except ImportError:
    _HAS_LITELLM = False

try:
    from crawl4ai import AsyncWebCrawler

    _HAS_CRAWL4AI = True
except ImportError:
    _HAS_CRAWL4AI = False

try:
    from genesis.web.fetch import WebFetcher

    _HAS_WEBFETCHER = True
except ImportError:
    _HAS_WEBFETCHER = False

try:
    from genesis.web.search import WebSearcher

    _HAS_WEBSEARCHER = True
except ImportError:
    _HAS_WEBSEARCHER = False

try:
    import diskcache

    _HAS_DISKCACHE = True
except ImportError:
    _HAS_DISKCACHE = False

logger = logging.getLogger("sweep_fetch")

# One-time dotenv loading flag
_dotenv_loaded = False


def _ensure_dotenv_loaded() -> None:
    """Load secrets.env once (for LLM API keys)."""
    global _dotenv_loaded  # noqa: PLW0603
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv

        secrets_path = REPO_DIR / "secrets.env"
        if secrets_path.exists():
            load_dotenv(secrets_path, override=False)
    except ImportError:
        pass

# ── ATS API endpoints (public, free, no auth) ────────────────────────
ATS_ENDPOINTS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}",
}

# ── LLM model mapping ─────────────────────────────────────────���──────
LADDER_TO_LITELLM: dict[str, str] = {
    "ollama_local": "ollama/qwen2.5:3b",
    "groq_llama": "groq/llama-3.3-70b-versatile",
    "gemini_flash": "gemini/gemini-2.0-flash",
    "claude_haiku": "anthropic/claude-haiku-4-5-20251001",
    "claude_sonnet": "anthropic/claude-sonnet-4-6-20250514",
}


# ══════════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════════


@dataclass
class SweepSpec:
    """Parsed research spec."""

    name: str
    goal: str
    owner_context: dict[str, Any]
    cost_controls: dict[str, Any]
    required_tiers: list[str]
    optional_tiers: list[str]
    tiers: dict[str, dict[str, Any]]
    extraction_schema: dict[str, Any]
    output: dict[str, str]
    reuse_prior_jds: dict[str, Any] | None = None
    spec_dir: Path = field(default_factory=lambda: Path("."))


@dataclass
class ATSJob:
    """Job discovered via ATS API."""

    title: str
    url: str
    company: str
    tier: str
    source_ats: str
    location: str = ""
    content_html: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedJD:
    """Fetched job description with content."""

    url: str
    company: str
    tier: str
    title: str
    markdown: str
    source: str  # "greenhouse_api" | "ashby_api" | "lever_api" | "web_crawl" | "web_fetch"


# ══════════════════════════════════════════════════════════════════════
# Page cache
# ══════════════════════════════════════════════════════════════════════


class PageCache:
    """Simple URL → content cache with TTL."""

    def __init__(self, ttl_hours: int = 168) -> None:
        self._ttl_s = ttl_hours * 3600
        if _HAS_DISKCACHE:
            cache_dir = Path.home() / ".genesis" / "sweep_cache"
            self._cache: diskcache.Cache | None = diskcache.Cache(str(cache_dir))
        else:
            self._cache = None
            logger.warning("diskcache not available, page caching disabled")

    def get(self, url: str) -> str | None:
        if self._cache is None:
            return None
        result = self._cache.get(url)
        return result if isinstance(result, str) else None

    def put(self, url: str, content: str) -> None:
        if self._cache is not None:
            self._cache.set(url, content, expire=self._ttl_s)


# ══════════════════════════════════════════════════════════════════════
# Spec parsing
# ══════════════════════════════════════════════════════════════════════


def parse_spec(path: Path) -> SweepSpec:
    """Parse a research spec YAML file into a SweepSpec."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    sweep = raw.get("sweep", {})
    if not sweep.get("name"):
        raise ValueError(f"Spec missing sweep.name: {path}")

    return SweepSpec(
        name=sweep["name"],
        goal=sweep.get("goal", ""),
        owner_context=sweep.get("owner_context", {}),
        cost_controls=raw.get("cost_controls", {}),
        required_tiers=raw.get("required_tiers", []),
        optional_tiers=raw.get("optional_tiers", []),
        tiers=raw.get("tiers", {}),
        extraction_schema=raw.get("per_jd_extraction_schema", {}),
        output=raw.get("output", {}),
        reuse_prior_jds=raw.get("reuse_prior_jds"),
        spec_dir=path.parent,
    )


# ══════════════════════════════════════════════════════════════════════
# ATS discovery helpers
# ══════════════════════════════════════════════════════════════════════


def generate_slugs(company_name: str) -> list[str]:
    """Generate ATS slug candidates from a company name."""
    base = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    base_nohyphen = base.replace("-", "")
    slugs = [base]
    if base_nohyphen != base:
        slugs.append(base_nohyphen)
    for suffix in ["-ai", "ai", "-jobs"]:
        candidate = base + suffix
        if candidate not in slugs:
            slugs.append(candidate)
    return slugs


def _title_matches_hints(title: str, hints: list[str]) -> bool:
    """Check if a job title matches any of the search hints."""
    title_lower = title.lower()
    return any(h.lower() in title_lower for h in hints)


async def try_greenhouse(
    client: httpx.AsyncClient, slug: str, hints: list[str]
) -> list[dict[str, Any]]:
    """Try Greenhouse API for a slug. Returns matching jobs or empty list."""
    url = ATS_ENDPOINTS["greenhouse"].format(slug=slug)
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs", [])
    return [
        {
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": j.get("location", {}).get("name", ""),
            "content_html": j.get("content", ""),
            "source_ats": "greenhouse",
            "raw": j,
        }
        for j in jobs
        if _title_matches_hints(j.get("title", ""), hints)
    ]


async def try_ashby(
    client: httpx.AsyncClient, slug: str, hints: list[str]
) -> list[dict[str, Any]]:
    """Try Ashby API for a slug."""
    url = ATS_ENDPOINTS["ashby"].format(slug=slug)
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs", [])
    return [
        {
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "location": j.get("location", ""),
            "content_html": "",
            "source_ats": "ashby",
            "raw": j,
        }
        for j in jobs
        if _title_matches_hints(j.get("title", ""), hints)
    ]


async def try_lever(
    client: httpx.AsyncClient, slug: str, hints: list[str]
) -> list[dict[str, Any]]:
    """Try Lever API for a slug."""
    url = ATS_ENDPOINTS["lever"].format(slug=slug)
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [
        {
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": j.get("categories", {}).get("location", ""),
            "content_html": "",
            "source_ats": "lever",
            "raw": j,
        }
        for j in data
        if _title_matches_hints(j.get("text", ""), hints)
    ]


# ══════════════════════════════════════════════════════════════════════
# Title normalization and dedup
# ══════════════════════════════════════════════════════════════════════

_LEVEL_NOISE = re.compile(
    r"\b(i{1,3}|iv|v|vi|sr\.?|senior|junior|jr\.?|staff|principal|lead|intern)\b",
    re.IGNORECASE,
)
_LOCATION_SUFFIX = re.compile(r"\s*[-–—]\s*(remote|hybrid|onsite).*$", re.IGNORECASE)
_PARENS = re.compile(r"\s*\(.*?\)\s*")


def normalize_title(title: str) -> str:
    """Normalize a job title for fuzzy comparison."""
    t = title.lower().strip()
    t = _LOCATION_SUFFIX.sub("", t)
    t = _PARENS.sub(" ", t)
    t = _LEVEL_NOISE.sub(" ", t)
    return " ".join(sorted(t.split()))


def fuzzy_match(a: str, b: str, threshold: float = 0.85) -> bool:
    """Check if two normalized titles are similar enough."""
    return SequenceMatcher(None, a, b).ratio() >= threshold


# ══════════════════════════════════════════════════════════════════════
# LLM extraction
# ══════════════════════════════════════════════════════════════════════


async def extract_fields(
    jd_markdown: str,
    schema: dict[str, Any],
    model_ladder: list[str],
    tokens_used: list[int],
) -> dict[str, Any] | None:
    """Extract structured fields from JD markdown using LLM."""
    if not _HAS_LITELLM:
        logger.error("litellm not installed, cannot extract fields")
        return None

    _ensure_dotenv_loaded()

    prompt = (
        "Extract the following fields from this job description. "
        "Return ONLY valid JSON with these keys, no markdown formatting.\n\n"
        f"Schema:\n{json.dumps(schema, indent=2)}\n\n"
        f"Job Description:\n{jd_markdown[:8000]}"
    )
    messages = [{"role": "user", "content": prompt}]

    for model_key in model_ladder:
        model_string = LADDER_TO_LITELLM.get(model_key, model_key)
        try:
            response = await litellm.acompletion(
                model=model_string,
                messages=messages,
                drop_params=True,
                temperature=0.0,
            )
            text = response.choices[0].message.content or ""
            # Track tokens
            usage = getattr(response, "usage", None)
            if usage:
                tokens_used[0] += getattr(usage, "total_tokens", 0)

            # Parse JSON — try direct, then extract from code block
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
                if match:
                    return json.loads(match.group(1))
                logger.warning(
                    "Model %s returned unparseable JSON, trying next", model_key
                )
                continue

        except Exception as exc:
            logger.warning("Model %s failed: %s, trying next", model_key, exc)
            continue

    logger.error("All models in ladder failed for extraction")
    return None


# ══════════════════════════════════════════════════════════════════════
# HTML to text helper
# ══════════════════════════════════════════════════════════════════════


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html)
    except ImportError:
        # Fallback: strip tags
        return re.sub(r"<[^>]+>", " ", html)


# ══════════════════════════════════════════════════════════════════════
# SweepRunner — main orchestrator
# ══════════════════════════════════════════════════════════════════════


class SweepRunner:
    """Orchestrates all sweep phases."""

    def __init__(self, spec: SweepSpec, *, dry_run: bool = False, output_dir: Path | None = None) -> None:
        self.spec = spec
        self.dry_run = dry_run
        self.output_dir = output_dir
        self.cache = PageCache(spec.cost_controls.get("page_cache_ttl_hours", 168))
        self.tokens_used: list[int] = [0]  # Mutable counter
        self._ats_sem = asyncio.Semaphore(5)
        self._fetch_sem = asyncio.Semaphore(10)
        self._llm_sem = asyncio.Semaphore(15)

    async def run(self) -> dict[str, Any]:
        """Execute all phases and return results."""
        logger.info("Starting sweep: %s", self.spec.name)

        if self.dry_run:
            return self._dry_run_report()

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # Phase 1: ATS discovery
            ats_jobs = await self._phase_ats_discovery(client)
            logger.info("Phase 1 complete: %d ATS jobs found", sum(len(v) for v in ats_jobs.values()))

            # Phase 2: Web discovery for companies without ATS hits
            web_urls = await self._phase_web_discovery(ats_jobs)
            logger.info("Phase 2 complete: %d web URLs found", sum(len(v) for v in web_urls.values()))

            # Phase 3: Fetch JD content
            fetched = await self._phase_jd_fetch(ats_jobs, web_urls)
            logger.info("Phase 3 complete: %d JDs fetched", len(fetched))

            # Phase 4: Dedup
            deduped = self._phase_dedup(fetched)
            logger.info("Phase 4 complete: %d unique JDs after dedup", len(deduped))

            # Phase 5: LLM extraction
            extracted = await self._phase_extraction(deduped)
            logger.info("Phase 5 complete: %d JDs extracted", len(extracted))

            # Phase 6: Reuse prior JDs
            if self.spec.reuse_prior_jds:
                reused = self._phase_reuse()
                extracted.extend(reused)
                logger.info("Phase 6 complete: %d prior JDs reused", len(reused))

            # Phase 7: Output
            self._phase_output(extracted)
            logger.info(
                "Sweep complete: %d JDs, %d tokens used",
                len(extracted),
                self.tokens_used[0],
            )

            return {"jds": extracted, "tokens_used": self.tokens_used[0]}

    def _dry_run_report(self) -> dict[str, Any]:
        """Print what would happen without executing."""
        tiers_to_run = self.spec.required_tiers + self.spec.optional_tiers
        total_companies = 0
        for tier_name in tiers_to_run:
            tier = self.spec.tiers.get(tier_name, {})
            companies = tier.get("companies", [])
            total_companies += len(companies)
            logger.info(
                "  Tier %s: %d companies, target %s JDs",
                tier_name,
                len(companies),
                tier.get("target_jds", "?"),
            )
        logger.info("Total: %d companies across %d tiers", total_companies, len(tiers_to_run))
        logger.info("Token budget: %s", self.spec.cost_controls.get("target_total_tokens", "unlimited"))
        logger.info("Reuse prior JDs: %s", "yes" if self.spec.reuse_prior_jds else "no")
        output = self.spec.output
        logger.info("Output JSON: %s", output.get("raw_json", "not specified"))
        logger.info("Synthesis: %s", output.get("synthesis", "not specified"))
        return {"dry_run": True, "total_companies": total_companies}

    # ── Phase 1: ATS Discovery ────────────────────────────────────────

    async def _phase_ats_discovery(
        self, client: httpx.AsyncClient
    ) -> dict[str, list[ATSJob]]:
        """Try ATS APIs for each company in each tier."""
        results: dict[str, list[ATSJob]] = {}
        tiers_to_run = self.spec.required_tiers + self.spec.optional_tiers

        tasks = []
        for tier_name in tiers_to_run:
            tier = self.spec.tiers.get(tier_name, {})
            hints = tier.get("title_search_hints", [])
            for company_entry in tier.get("companies", []):
                company_name, explicit_slugs = self._parse_company_entry(company_entry)
                tasks.append(
                    self._discover_company_ats(
                        client, company_name, tier_name, hints, explicit_slugs
                    )
                )

        for coro in asyncio.as_completed(tasks):
            company_name, tier_name, jobs = await coro
            key = f"{tier_name}:{company_name}"
            if jobs:
                results[key] = jobs

        return results

    async def _discover_company_ats(
        self,
        client: httpx.AsyncClient,
        company: str,
        tier: str,
        hints: list[str],
        explicit_slugs: dict[str, str],
    ) -> tuple[str, str, list[ATSJob]]:
        """Try all ATS providers for a single company."""
        async with self._ats_sem:
            jobs: list[ATSJob] = []
            ats_funcs = {
                "greenhouse": try_greenhouse,
                "ashby": try_ashby,
                "lever": try_lever,
            }

            for ats_name, func in ats_funcs.items():
                slug_key = f"{ats_name}_slug"
                if slug_key in explicit_slugs:
                    slugs = [explicit_slugs[slug_key]]
                else:
                    slugs = generate_slugs(company)

                for slug in slugs:
                    try:
                        raw_jobs = await func(client, slug, hints)
                        for rj in raw_jobs:
                            jobs.append(
                                ATSJob(
                                    title=rj["title"],
                                    url=rj["url"],
                                    company=company,
                                    tier=tier,
                                    source_ats=rj["source_ats"],
                                    location=rj.get("location", ""),
                                    content_html=rj.get("content_html", ""),
                                    raw=rj.get("raw", {}),
                                )
                            )
                        if raw_jobs:
                            logger.info(
                                "  ATS hit: %s/%s → %d jobs for %s",
                                ats_name,
                                slug,
                                len(raw_jobs),
                                company,
                            )
                            break  # Found jobs on this ATS, skip other slugs
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            logger.warning(
                                "ATS %s/%s returned %d", ats_name, slug, exc.response.status_code
                            )
                    except httpx.HTTPError as exc:
                        logger.warning("ATS %s/%s error: %s", ats_name, slug, exc)

                    # Small delay between slug attempts
                    await asyncio.sleep(0.3)

            return company, tier, jobs

    # ── Phase 2: Web Discovery ────────────────────────────────────────

    async def _phase_web_discovery(
        self, ats_results: dict[str, list[ATSJob]]
    ) -> dict[str, list[str]]:
        """Search web for companies without ATS hits."""
        if not _HAS_WEBSEARCHER:
            logger.warning("WebSearcher not available, skipping web discovery")
            return {}

        searcher = WebSearcher()
        results: dict[str, list[str]] = {}
        tiers_to_run = self.spec.required_tiers + self.spec.optional_tiers

        for tier_name in tiers_to_run:
            tier = self.spec.tiers.get(tier_name, {})
            hints = tier.get("title_search_hints", [])

            for company_entry in tier.get("companies", []):
                company_name, explicit = self._parse_company_entry(company_entry)
                key = f"{tier_name}:{company_name}"

                # Skip if ATS already found jobs
                if key in ats_results:
                    continue

                # If company has a careers_url, use that directly
                if "careers_url" in explicit:
                    results[key] = [explicit["careers_url"]]
                    continue

                # Search web
                urls: list[str] = []
                for hint in hints[:3]:  # Limit to 3 hints per company
                    query = f'"{company_name}" "{hint}" job'
                    try:
                        resp = await searcher.search(query, max_results=5)
                        for sr in resp.results:
                            # Filter for likely JD URLs (not landing pages)
                            if self._is_jd_url(sr.url):
                                urls.append(sr.url)
                    except Exception as exc:
                        logger.warning("Search failed for %s: %s", company_name, exc)

                if urls:
                    results[key] = list(dict.fromkeys(urls))[:10]  # Dedup, cap at 10
                    logger.info("  Web: %d URLs for %s", len(results[key]), company_name)

        return results

    # ── Phase 3: JD Fetch ─────────────────────────────────────────────

    async def _phase_jd_fetch(
        self,
        ats_results: dict[str, list[ATSJob]],
        web_urls: dict[str, list[str]],
    ) -> list[FetchedJD]:
        """Fetch JD content for all discovered jobs."""
        fetched: list[FetchedJD] = []

        # Convert ATS jobs — inline HTML or queue for fetch
        ats_fetch_tasks = []
        for _key, jobs in ats_results.items():
            for job in jobs:
                if job.content_html:
                    # Greenhouse includes full JD HTML — extract text directly
                    markdown = _html_to_text(job.content_html)
                    fetched.append(
                        FetchedJD(
                            url=job.url,
                            company=job.company,
                            tier=job.tier,
                            title=job.title,
                            markdown=markdown,
                            source=f"{job.source_ats}_api",
                        )
                    )
                else:
                    # Queue for concurrent fetch
                    ats_fetch_tasks.append(
                        self._fetch_ats_job(job)
                    )

        # Fetch ATS jobs without inline content (concurrent)
        for coro in asyncio.as_completed(ats_fetch_tasks):
            result = await coro
            if result:
                fetched.append(result)

        # Fetch web-discovered URLs (concurrent)
        fetch_tasks = []
        for key, urls in web_urls.items():
            tier, company = key.split(":", 1)
            for url in urls:
                fetch_tasks.append(self._fetch_and_wrap(url, company, tier))

        for coro in asyncio.as_completed(fetch_tasks):
            result = await coro
            if result:
                fetched.append(result)

        return fetched

    async def _fetch_ats_job(self, job: ATSJob) -> FetchedJD | None:
        """Fetch an ATS job page that lacks inline content."""
        content, source = await self._fetch_url(job.url)
        if not content:
            return None
        return FetchedJD(
            url=job.url, company=job.company, tier=job.tier,
            title=job.title, markdown=content,
            source=f"{job.source_ats}_api",
        )

    async def _fetch_and_wrap(
        self, url: str, company: str, tier: str
    ) -> FetchedJD | None:
        """Fetch a URL and wrap in FetchedJD."""
        content, source = await self._fetch_url(url)
        if not content:
            return None
        # Extract title from first heading or URL
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1) if title_match else url.split("/")[-1]
        return FetchedJD(
            url=url, company=company, tier=tier,
            title=title, markdown=content, source=source,
        )

    async def _fetch_url(self, url: str) -> tuple[str | None, str]:
        """Fetch a URL with cache and fallback chain. Returns (content, source)."""
        # Check cache
        cached = self.cache.get(url)
        if cached:
            return cached, "cache"

        async with self._fetch_sem:
            # Try Crawl4AI first (JS rendering)
            if _HAS_CRAWL4AI:
                try:
                    async with AsyncWebCrawler() as crawler:
                        result = await crawler.arun(url=url)
                    content = result.markdown or ""
                    if content:
                        self.cache.put(url, content)
                        return content, "web_crawl"
                except Exception as exc:
                    logger.warning("Crawl4AI failed for %s: %s", url, exc)

            # Fallback to WebFetcher
            if _HAS_WEBFETCHER:
                try:
                    fetcher = WebFetcher()
                    result = await fetcher.fetch(url)
                    if result.text:
                        self.cache.put(url, result.text)
                        return result.text, "web_fetch"
                except Exception as exc:
                    logger.warning("WebFetcher failed for %s: %s", url, exc)

            logger.warning("All fetchers failed for %s", url)
            return None, ""

    # ── Phase 4: Dedup ────────────────────────────────────────────────

    def _phase_dedup(self, jds: list[FetchedJD]) -> list[FetchedJD]:
        """Deduplicate by (company, normalized_title)."""
        # Source priority: ATS API > web_crawl > web_fetch
        source_priority = {"greenhouse_api": 3, "ashby_api": 3, "lever_api": 3, "web_crawl": 2, "web_fetch": 1}

        # Group by company
        by_company: dict[str, list[FetchedJD]] = {}
        for jd in jds:
            by_company.setdefault(jd.company, []).append(jd)

        kept: list[FetchedJD] = []
        for company, company_jds in by_company.items():
            seen: list[tuple[str, FetchedJD]] = []  # (normalized_title, jd)
            for jd in company_jds:
                norm = normalize_title(jd.title)
                is_dup = False
                for i, (existing_norm, existing_jd) in enumerate(seen):
                    if fuzzy_match(norm, existing_norm):
                        # Keep higher-priority source
                        new_prio = source_priority.get(jd.source, 0)
                        old_prio = source_priority.get(existing_jd.source, 0)
                        if new_prio > old_prio:
                            seen[i] = (norm, jd)
                            logger.debug(
                                "Dedup: replaced %s with %s for %s at %s",
                                existing_jd.source, jd.source, jd.title, company,
                            )
                        is_dup = True
                        break
                if not is_dup:
                    seen.append((norm, jd))

            kept.extend(jd for _, jd in seen)

        removed = len(jds) - len(kept)
        if removed:
            logger.info("Dedup removed %d duplicates", removed)
        return kept

    # ── Phase 5: LLM Extraction ───────────────────────────────────────

    async def _phase_extraction(self, jds: list[FetchedJD]) -> list[dict[str, Any]]:
        """Extract structured fields from each JD using LLM."""
        ladder = self.spec.cost_controls.get("provider_fallback_ladder", ["gemini_flash"])
        token_budget = self.spec.cost_controls.get("target_total_tokens")
        results: list[dict[str, Any]] = []

        async def extract_one(jd: FetchedJD) -> dict[str, Any] | None:
            async with self._llm_sem:
                # Token budget warning (observability, not control)
                if token_budget and self.tokens_used[0] > token_budget * 0.8:
                    logger.warning(
                        "Token usage at %d/%d (%.0f%%)",
                        self.tokens_used[0],
                        token_budget,
                        self.tokens_used[0] / token_budget * 100,
                    )

                extracted = await extract_fields(
                    jd.markdown, self.spec.extraction_schema, ladder, self.tokens_used
                )
                if extracted:
                    # Enrich with metadata
                    extracted["_company"] = jd.company
                    extracted["_tier"] = jd.tier
                    extracted["_url"] = jd.url
                    extracted["_source"] = jd.source
                    extracted["_title_from_source"] = jd.title
                    extracted["_fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    return extracted
                return {"_extraction_failed": True, "_company": jd.company, "_url": jd.url, "_tier": jd.tier}

        tasks = [extract_one(jd) for jd in jds]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result:
                results.append(result)

        return results

    # ── Phase 6: Reuse Prior JDs ──────────────────────────────────────

    def _phase_reuse(self) -> list[dict[str, Any]]:
        """Load and re-classify prior sweep JDs."""
        cfg = self.spec.reuse_prior_jds
        if not cfg:
            return []

        source_path = self.spec.spec_dir / cfg.get("source", "")
        if not source_path.exists():
            logger.warning("Prior JD source not found: %s", source_path)
            return []

        try:
            with open(source_path, encoding="utf-8") as f:
                prior_jds = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load prior JDs: %s", exc)
            return []

        filters = cfg.get("filter", [])
        matched = []
        for jd in prior_jds:
            for flt in filters:
                company_match = flt.get("company", "").lower() in jd.get("company", "").lower()
                title_match = flt.get("title_contains", "").lower() in jd.get("title_exact", jd.get("title", "")).lower()
                if company_match and title_match:
                    matched.append({
                        **jd,
                        "_source": "prior_sweep",
                        "_reused_from": str(source_path),
                    })
                    break

        logger.info("Reused %d/%d prior JDs matching filters", len(matched), len(prior_jds))
        return matched

    # ── Phase 7: Output ────────────────────────────────────────��──────

    def _phase_output(self, extracted: list[dict[str, Any]]) -> None:
        """Write output files."""
        output_cfg = self.spec.output
        base_dir = self.output_dir or self.spec.spec_dir

        # Raw JSON
        raw_json_path = base_dir / output_cfg.get("raw_json", f"data/{self.spec.name}-jds.json")
        raw_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump(extracted, f, indent=2, ensure_ascii=False)
        logger.info("Wrote %d JDs to %s", len(extracted), raw_json_path)

        # Per-tier narratives
        for narrative_path_str in output_cfg.get("per_tier_narratives", []):
            narrative_path = base_dir / narrative_path_str
            tier_name = narrative_path.stem
            tier_jds = [j for j in extracted if tier_name.replace("-", "_") in j.get("_tier", "")]
            narrative_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_narrative(narrative_path, tier_name, tier_jds)

        # Synthesis
        synthesis_path_str = output_cfg.get("synthesis")
        if synthesis_path_str:
            synthesis_path = base_dir / synthesis_path_str
            synthesis_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_synthesis(synthesis_path, extracted)

    def _write_narrative(self, path: Path, tier_name: str, jds: list[dict[str, Any]]) -> None:
        """Write a per-tier narrative markdown file."""
        lines = [f"# {tier_name.replace('-', ' ').title()}\n"]
        lines.append(f"**{len(jds)} JDs collected**\n\n")

        if not jds:
            lines.append("No JDs found for this tier.\n")
        else:
            lines.append("| Company | Title | Location | Remote | Source |\n")
            lines.append("|---------|-------|----------|--------|--------|\n")
            for jd in sorted(jds, key=lambda j: j.get("_company", "")):
                company = jd.get("_company", jd.get("company", "?"))
                title = jd.get("title_exact", jd.get("_title_from_source", "?"))
                location = jd.get("location", "?")
                remote = jd.get("remote_policy", "?")
                source = jd.get("_source", "?")
                lines.append(f"| {company} | {title} | {location} | {remote} | {source} |\n")

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        logger.info("Wrote narrative: %s (%d JDs)", path, len(jds))

    def _write_synthesis(self, path: Path, jds: list[dict[str, Any]]) -> None:
        """Write synthesis markdown file."""
        lines = [f"# {self.spec.name.replace('_', ' ').title()} — Synthesis\n\n"]
        lines.append(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n")
        lines.append(f"**Total JDs:** {len(jds)}\n")
        lines.append(f"**Tokens used:** {self.tokens_used[0]}\n\n")

        # Per-tier summary
        tiers: dict[str, list[dict[str, Any]]] = {}
        for jd in jds:
            tier = jd.get("_tier", "unknown")
            tiers.setdefault(tier, []).append(jd)

        lines.append("## Per-Tier Summary\n\n")
        for tier_name, tier_jds in sorted(tiers.items()):
            lines.append(f"### {tier_name}\n")
            lines.append(f"- **JDs found:** {len(tier_jds)}\n")
            companies = sorted(set(j.get("_company", "?") for j in tier_jds))
            lines.append(f"- **Companies:** {', '.join(companies)}\n")
            failed = sum(1 for j in tier_jds if j.get("_extraction_failed"))
            if failed:
                lines.append(f"- **Extraction failures:** {failed}\n")
            lines.append("\n")

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        logger.info("Wrote synthesis: %s", path)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_company_entry(entry: Any) -> tuple[str, dict[str, str]]:
        """Parse a company entry from the spec (string or dict)."""
        if isinstance(entry, str):
            return entry, {}
        if isinstance(entry, dict):
            name = entry.get("name", str(entry))
            explicit = {
                k: v
                for k, v in entry.items()
                if k in ("greenhouse_slug", "ashby_slug", "lever_slug", "careers_url")
            }
            return name, explicit
        return str(entry), {}

    @staticmethod
    def _is_jd_url(url: str) -> bool:
        """Heuristic: is this URL likely an individual JD, not a landing page?"""
        # URLs with job IDs or specific posting paths
        patterns = [
            r"/jobs/\d+",
            r"/postings/[^/]+/[^/]+",
            r"/job/[^/]+",
            r"/position/",
            r"/opening/",
            r"/careers/[^/]+/[^/]+",  # /careers/company/role-slug
            r"jobs\.lever\.co/[^/]+/[^/]+",  # Lever: /company/role-id
            r"boards\.greenhouse\.io/[^/]+/jobs/",  # Greenhouse board URLs
            r"jobs\.ashbyhq\.com/[^/]+/[^/]+",  # Ashby: /company/role-id
        ]
        return any(re.search(p, url) for p in patterns)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════���══


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a structured job-description sweep from a research spec."
    )
    parser.add_argument("spec", type=Path, help="Path to research spec YAML")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--output-dir", type=Path, help="Override output directory")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.spec.exists():
        logger.error("Spec file not found: %s", args.spec)
        sys.exit(1)

    spec = parse_spec(args.spec)
    runner = SweepRunner(spec, dry_run=args.dry_run, output_dir=args.output_dir)
    result = asyncio.run(runner.run())

    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"\nSweep complete: {len(result.get('jds', []))} JDs, {result.get('tokens_used', 0)} tokens")


if __name__ == "__main__":
    main()
