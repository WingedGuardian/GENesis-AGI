"""Model Intelligence Job — scan model landscape, detect changes.

Runs weekly (Sundays 6am, per config/recon_schedules.yaml) or on-demand
via recon MCP tool. Checks OpenRouter for new/changed models, detects new
free models, enriches profiles from ArtificialAnalysis.ai, flags stale
profiles, and stores findings for strategic reflection review.

Free model inventory runs daily (free_model_inventory schedule) via
_check_free_models(). New free models create follow-up records with
strategy='surplus_task' — the follow-up dispatcher handles actual
MODEL_EVAL enqueueing and tracks benchmark lifecycle to completion.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import httpx

if TYPE_CHECKING:
    from genesis.routing.model_profiles import ModelProfileRegistry
    from genesis.surplus.queue import SurplusQueue

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_AA_MODELS_URL = "https://artificialanalysis.ai/api/data/llms/models"
_FREE_MODEL_CACHE_PATH = Path.home() / ".genesis" / "free_model_cache.json"

# ── Active-model EOL detection (v1: Groq deprecations page) ────────────────
_GROQ_DEPRECATIONS_URL = "https://console.groq.com/docs/deprecations"
# A Groq deprecations table row is 3 pipe-separated cells. Rendered text may
# carry surrounding pipes (`| model | date | repl |`, MCP-style markdown) or
# not (`model| date| repl`, what genesis.web.fetch returns) — handle both.
_DEP_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _parse_groq_deprecation_table(text: str) -> list[tuple[str, str, str]]:
    """Parse Groq's deprecations markdown table into rows of
    ``(model_id, shutdown_date_iso, replacement)``.

    Only rows whose middle column carries a ``MM/DD/YY`` date count as
    deprecation entries; header / separator / other rows are skipped. Robust
    to missing backticks. Returns ``[]`` when nothing parses (the caller logs
    a structure-changed warning).
    """
    rows: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if "|" not in line:
            continue
        # Strip surrounding pipes (markdown style) then split into cells.
        parts = [c.strip() for c in line.strip("|").split("|")]
        if len(parts) != 3:
            continue
        model_cell, date_cell, repl_cell = parts
        date_m = _DEP_DATE_RE.search(date_cell)
        if not date_m:
            continue  # header / separator / non-date row
        model_id = model_cell.strip("` ").strip()
        if not model_id or model_id.lower() == "deprecated model":
            continue
        mm, dd, yy = date_m.groups()
        mm_int, dd_int = int(mm), int(dd)
        if not (1 <= mm_int <= 12 and 1 <= dd_int <= 31):
            continue  # not a real calendar date (e.g. a version number)
        year = int(yy) + 2000 if int(yy) < 100 else int(yy)
        shutdown_iso = f"{year:04d}-{mm_int:02d}-{dd_int:02d}"
        replacement = repl_cell.replace("`", "").strip()
        rows.append((model_id, shutdown_iso, replacement))
    return rows


def _eol_findings_from_rows(
    rows: list[tuple[str, str, str]], providers: dict, call_sites: dict,
) -> list[dict]:
    """Match parsed deprecation rows against our ACTIVE Groq providers.

    Emits an ``active_model_eol`` finding only for a model we actually use
    (firehose guard: an unrelated Groq deprecation yields nothing), annotated
    with blast radius — the call-site chains that depend on that provider.
    """
    provider_chains: dict[str, list[str]] = defaultdict(list)
    for site_id, site in call_sites.items():
        for prov in site.chain:
            provider_chains[prov].append(site_id)
    groq_models = {
        p.model_id: name
        for name, p in providers.items()
        if p.enabled and p.provider_type == "groq"
    }
    findings: list[dict] = []
    for model_id, shutdown_iso, replacement in rows:
        name = groq_models.get(model_id)
        if name is None:
            continue
        findings.append({
            "type": "active_model_eol",
            "model": model_id,
            "provider": name,
            "vendor": "groq",
            "shutdown_date": shutdown_iso,
            "replacement": replacement,
            "blast_radius": sorted(set(provider_chains.get(name, []))),
            "source": "groq_deprecations_page",
        })
    return findings


class ModelIntelligenceJob:
    """Scans the model landscape and compares against known profiles."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        profile_registry: ModelProfileRegistry | None = None,
        profiles_path: Path | None = None,
        surplus_queue: SurplusQueue | None = None,
    ):
        self._db = db
        self._registry = profile_registry
        self._profiles_path = profiles_path
        self._surplus_queue = surplus_queue

    async def run(self) -> dict:
        """Run the full model intelligence scan.

        Returns summary dict with findings count and categories.
        """
        findings: list[dict] = []

        # 1. OpenRouter model check (pricing changes, new high-context models)
        or_findings = await self._check_openrouter()
        findings.extend(or_findings)

        # 2. Free model inventory (new free models → MODEL_EVAL surplus tasks)
        free_findings = await self._check_free_models()
        findings.extend(free_findings)

        # 3. ArtificialAnalysis.ai benchmark enrichment
        aa_findings = await self._enrich_from_artificialanalysis()
        findings.extend(aa_findings)

        # 4. Profile staleness check
        stale_findings = await self._check_staleness()
        findings.extend(stale_findings)

        # 5. Active-provider EOL check (models WE USE being deprecated)
        eol_findings = await self._check_active_providers()
        findings.extend(eol_findings)

        # 6. Store all findings
        for f in findings:
            await self._store_finding(f)

        logger.info(
            "Model intelligence scan complete: %d findings", len(findings),
        )
        return {
            "total_findings": len(findings),
            "openrouter_findings": len(or_findings),
            "free_model_findings": len(free_findings),
            "aa_findings": len(aa_findings),
            "stale_findings": len(stale_findings),
            "eol_findings": len(eol_findings),
            "findings": findings,
        }

    async def _check_openrouter(self) -> list[dict]:
        """Check OpenRouter API for model changes."""
        findings: list[dict] = []
        data: dict = {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(_OPENROUTER_MODELS_URL)
                if resp.status_code != 200:
                    logger.warning("OpenRouter API returned %d", resp.status_code)
                    return findings
                data = resp.json()
        except Exception:
            logger.warning("Failed to fetch OpenRouter models", exc_info=True)
            return findings

        models = data.get("data", [])
        if not models:
            return findings

        # Build lookup of known models by api_id
        known_ids: dict[str, str] = {}
        if self._registry:
            for name, profile in self._registry.all_profiles().items():
                known_ids[profile.api_id] = name

        for model in models:
            model_id = model.get("id", "")
            # Check if this is a model we track
            if model_id in known_ids:
                profile_name = known_ids[model_id]
                profile = self._registry.get(profile_name) if self._registry else None
                if profile:
                    # Check for pricing changes
                    try:
                        pricing = model.get("pricing", {})
                        or_input = float(pricing.get("prompt", 0)) * 1_000_000
                        or_output = float(pricing.get("completion", 0)) * 1_000_000
                        if (
                            abs(or_input - profile.cost_per_mtok_in) > 0.01
                            or abs(or_output - profile.cost_per_mtok_out) > 0.01
                        ):
                            findings.append({
                                "type": "pricing_change",
                                "model": profile_name,
                                "api_id": model_id,
                                "old_pricing": {
                                    "input": profile.cost_per_mtok_in,
                                    "output": profile.cost_per_mtok_out,
                                },
                                "new_pricing": {
                                    "input": round(or_input, 4),
                                    "output": round(or_output, 4),
                                },
                            })
                    except (ValueError, TypeError):
                        logger.warning("Failed to parse pricing for %s", model_id, exc_info=True)
            else:
                # New model detection: free models always flagged, paid only if ≥100k context
                pricing = model.get("pricing", {})
                is_free = False
                with contextlib.suppress(ValueError, TypeError):
                    is_free = float(pricing.get("prompt", -1)) == 0
                context = model.get("context_length", 0)
                if is_free or context >= 100_000:
                    findings.append({
                        "type": "new_model",
                        "api_id": model_id,
                        "name": model.get("name", model_id),
                        "context_length": context,
                        "pricing": pricing,
                        "is_free": is_free,
                    })

        return findings

    async def _check_free_models(self) -> list[dict]:
        """Detect new free models on OpenRouter and enqueue MODEL_EVAL tasks.

        Maintains a local cache (~/.genesis/free_model_cache.json) of known
        free model IDs. On each run, fetches the current free model list from
        OpenRouter, diffs against cache, and:
        - Produces findings for newly appeared free models
        - Enqueues MODEL_EVAL surplus tasks (if surplus_queue is available)
        - Produces a summary observation with the full free model inventory
        """
        findings: list[dict] = []
        data: dict = {}

        # Fetch models from OpenRouter (reuse the same endpoint)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(_OPENROUTER_MODELS_URL)
                if resp.status_code != 200:
                    logger.warning("OpenRouter API returned %d for free model check", resp.status_code)
                    return findings
                data = resp.json()
        except Exception:
            logger.warning("Failed to fetch OpenRouter models for free check", exc_info=True)
            return findings

        # Extract free models (pricing.prompt == "0" or == 0)
        current_free: dict[str, dict] = {}
        for model in data.get("data", []):
            pricing = model.get("pricing", {})
            try:
                if float(pricing.get("prompt", -1)) == 0:
                    model_id = model.get("id", "")
                    if model_id:
                        current_free[model_id] = {
                            "name": model.get("name", model_id),
                            "context_length": model.get("context_length", 0),
                            "created": model.get("created"),
                        }
            except (ValueError, TypeError):
                continue

        # Load cache
        cached_ids: set[str] = set()
        if _FREE_MODEL_CACHE_PATH.exists():
            try:
                cached_data = json.loads(_FREE_MODEL_CACHE_PATH.read_text())
                cached_ids = set(cached_data.get("model_ids", []))
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt free model cache, rebuilding")

        # Diff: new models not in cache
        new_ids = set(current_free.keys()) - cached_ids
        removed_ids = cached_ids - set(current_free.keys())

        if new_ids:
            logger.info("Detected %d new free models on OpenRouter", len(new_ids))

        for model_id in sorted(new_ids):
            info = current_free[model_id]
            findings.append({
                "type": "new_free_model",
                "api_id": model_id,
                "name": info["name"],
                "context_length": info["context_length"],
                "created": info.get("created"),
            })
            # Create follow-up to track benchmark lifecycle
            await self._create_benchmark_follow_up(model_id, info)

        # Report removed free models (went paid or deprecated)
        for model_id in sorted(removed_ids):
            findings.append({
                "type": "free_model_removed",
                "api_id": model_id,
            })

        # Produce inventory summary observation
        if current_free:
            findings.append({
                "type": "free_model_inventory",
                "total_free": len(current_free),
                "new_count": len(new_ids),
                "removed_count": len(removed_ids),
                "top_context": sorted(
                    current_free.items(),
                    key=lambda x: x[1].get("context_length", 0),
                    reverse=True,
                )[:10],
            })

        # Update cache
        try:
            _FREE_MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _FREE_MODEL_CACHE_PATH.write_text(json.dumps({
                "model_ids": sorted(current_free.keys()),
                "updated_at": datetime.now(UTC).isoformat(),
                "total": len(current_free),
            }, indent=2))
        except OSError:
            logger.warning("Failed to write free model cache", exc_info=True)

        return findings

    async def _enrich_from_artificialanalysis(self) -> list[dict]:
        """Fetch benchmark data from ArtificialAnalysis.ai API.

        Free tier: 1,000 requests/day. Returns MMLU-Pro, GPQA, HLE,
        LiveCodeBench, MATH-500, AIME scores plus speed/latency metrics.

        Produces enrichment findings for models we have profiles for.
        Does NOT modify profile YAML directly (that's a Tier 2 action).
        """
        findings: list[dict] = []
        data: dict | list = {}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(_AA_MODELS_URL)
                if resp.status_code != 200:
                    logger.warning("ArtificialAnalysis API returned %d", resp.status_code)
                    return findings
                data = resp.json()
        except Exception:
            logger.warning("Failed to fetch ArtificialAnalysis data", exc_info=True)
            return findings

        # AA returns a list of model objects with benchmark scores
        models = data if isinstance(data, list) else data.get("data", data.get("models", []))
        if not models:
            logger.info("ArtificialAnalysis returned empty model list")
            return findings

        # Build lookup of our profiled models by display_name (fuzzy match)
        profile_lookup: dict[str, str] = {}
        if self._registry:
            for name, profile in self._registry.all_profiles().items():
                # Index by lowercased display name and api_id for matching
                profile_lookup[profile.display_name.lower()] = name
                profile_lookup[profile.api_id.lower()] = name

        enriched_count = 0
        for model in models:
            model_name = model.get("name", "")
            model_key = model.get("key", "")

            # Try to match against our profiles
            matched_profile = (
                profile_lookup.get(model_name.lower())
                or profile_lookup.get(model_key.lower())
            )

            # Extract benchmark scores (field names may vary — be defensive)
            benchmarks = {}
            for field_name in ("mmlu_pro", "gpqa", "hle", "livecodebench", "math_500", "aime",
                               "humaneval", "swe_bench", "arena_elo"):
                val = model.get(field_name)
                if val is not None:
                    benchmarks[field_name] = val

            # Also check nested "benchmarks" dict if present
            if "benchmarks" in model and isinstance(model["benchmarks"], dict):
                for k, v in model["benchmarks"].items():
                    if v is not None and k not in benchmarks:
                        benchmarks[k] = v

            speed_data = {}
            for field_name in ("tokens_per_second", "time_to_first_token", "latency_ms"):
                val = model.get(field_name)
                if val is not None:
                    speed_data[field_name] = val

            if benchmarks and matched_profile:
                findings.append({
                    "type": "benchmark_enrichment",
                    "model": matched_profile,
                    "source_name": model_name,
                    "benchmarks": benchmarks,
                    "speed": speed_data,
                    "source": "artificialanalysis",
                })
                enriched_count += 1
            elif benchmarks and not matched_profile:
                # Unknown model with benchmarks — record for awareness
                findings.append({
                    "type": "benchmark_unmatched",
                    "source_name": model_name,
                    "source_key": model_key,
                    "benchmarks": benchmarks,
                    "source": "artificialanalysis",
                })

        if enriched_count:
            logger.info("Enriched %d profiles from ArtificialAnalysis", enriched_count)

        return findings

    async def _check_staleness(self) -> list[dict]:
        """Check for profiles that haven't been reviewed recently."""
        if not self._registry:
            return []

        stale = self._registry.stale_profiles(days=30)
        findings: list[dict] = []
        for profile in stale:
            findings.append({
                "type": "stale_profile",
                "model": profile.name,
                "last_reviewed": profile.last_reviewed or "never",
                "provider": profile.provider,
            })
        return findings

    async def _store_finding(self, finding: dict) -> str:
        """Store a model intelligence finding via intake pipeline."""
        import uuid

        finding_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        finding_type = finding.get("type", "unknown")

        # active_model_eol is a HIGH-priority, actionable alert — surface it as
        # a recon observation, NOT via run_intake (curated model-intelligence
        # findings route to the knowledge base, which would bury it).
        if finding_type == "active_model_eol":
            return await self._surface_eol_observation(finding, finding_id, now)

        title = f"Model intelligence: {finding_type}"
        if "model" in finding:
            title += f" — {finding['model']}"
        elif "api_id" in finding:
            title += f" — {finding['api_id']}"

        # GROUNDWORK(V4): Extend this dict with call_site + outcome for performance tracking
        content = json.dumps({
            "title": title,
            **finding,
            "detected_at": now,
        })

        try:
            from genesis.surplus.intake import IntakeSource, run_intake
            await run_intake(
                content=content,
                source=IntakeSource.MODEL_INTELLIGENCE,
                source_task_type="model_intelligence",
                db=self._db,
            )
        except Exception:
            # Fallback: store as observation directly (old behavior)
            logger.warning("Intake failed for model_intelligence finding — falling back to direct observation", exc_info=True)
            _MEDIUM_TYPES = ("pricing_change", "new_model", "new_free_model", "free_model_removed")
            priority = "medium" if finding_type in _MEDIUM_TYPES else "low"

            from genesis.db.crud import observations
            await observations.create(
                self._db,
                id=finding_id,
                source="recon",
                type="finding",
                content=content,
                priority=priority,
                created_at=now,
                category="model_intelligence",
            )
        return finding_id

    async def _create_benchmark_follow_up(
        self, model_id: str, info: dict,
    ) -> str | None:
        """Create a follow-up to benchmark a newly detected free model.

        The follow-up dispatcher will enqueue the MODEL_EVAL surplus task
        and track it through to completion. This replaces direct surplus
        enqueueing so the benchmark lifecycle is visible and accountable.
        """
        from genesis.db.crud import follow_ups as follow_up_crud

        # Respect the same cap as before: max 10 pending MODEL_EVAL tasks
        if self._surplus_queue is not None:
            try:
                from genesis.surplus.types import TaskType

                pending = await self._surplus_queue.pending_by_type(TaskType.MODEL_EVAL)
                if pending >= 10:
                    logger.info(
                        "MODEL_EVAL queue already has %d pending, skipping follow-up for %s",
                        pending, model_id,
                    )
                    return None
            except Exception:
                logger.warning("Failed to check MODEL_EVAL pending count", exc_info=True)

        try:
            payload = {
                "task_type": "model_eval",
                "compute_tier": "free_api",
                "payload": {
                    "model_id": model_id,
                    "name": info["name"],
                    "source": "openrouter_free_scan",
                },
            }
            fid = await follow_up_crud.create(
                self._db,
                content=f"Benchmark new free model: {model_id} ({info['name']})",
                source="recon_pipeline",
                strategy="surplus_task",
                reason=json.dumps(payload),
                priority="medium",
                domain="internal",
            )
            logger.info("Created benchmark follow-up %s for %s", fid[:8], model_id)
            return fid
        except Exception:
            logger.warning("Failed to create benchmark follow-up for %s", model_id, exc_info=True)
            return None

    async def _check_active_providers(self) -> list[dict]:
        """Detect EOL of models WE ACTUALLY USE (v1: Groq deprecations page).

        Loads the effective routing config to learn our active Groq models and
        the chains that depend on each provider (blast radius), parses Groq's
        structured deprecations table, and emits an ``active_model_eol``
        finding ONLY for our active Groq models that appear with a shutdown
        date.

        Firehose-proof by construction: a deprecation for a Groq model we
        don't use yields nothing; a fetch/parse failure yields nothing
        (degrade gracefully, never crash the scan). Catalog ``/v1/models``
        diff (all OpenAI-compatible vendors) and the Google changelog are a
        documented follow-up, not silent gaps.
        """
        try:
            from genesis.env import repo_root
            from genesis.routing.config import load_config

            cfg = load_config(
                repo_root() / "config" / "model_routing.yaml",
                check_api_keys=False,
            )
        except Exception:
            logger.warning(
                "EOL check: could not load routing config — skipping", exc_info=True,
            )
            return []

        groq_count = sum(
            1 for p in cfg.providers.values()
            if p.enabled and p.provider_type == "groq"
        )
        if groq_count == 0:
            return []

        rows = await self._fetch_groq_deprecations()
        findings = _eol_findings_from_rows(rows, cfg.providers, cfg.call_sites)

        if findings:
            logger.warning(
                "EOL check: %d active Groq model(s) deprecated: %s",
                len(findings), [f["model"] for f in findings],
            )
        logger.info(
            "EOL check: scanned Groq deprecations for %d active Groq model(s); "
            "catalog-diff + non-Groq vendors deferred to follow-up",
            groq_count,
        )
        return findings

    async def _fetch_groq_deprecations(self) -> list[tuple[str, str, str]]:
        """Fetch + parse Groq's deprecations page. Fails safe → [] on any error.

        Uses ``genesis.web.fetch`` (rendered fetch with anti-bot/JS handling),
        because the Groq docs page is JS-rendered and raw httpx would not see
        the table.
        """
        try:
            from genesis.web import fetch

            result = await fetch(_GROQ_DEPRECATIONS_URL, max_chars=40000)
        except Exception:
            logger.warning("EOL check: Groq deprecations fetch raised", exc_info=True)
            return []
        if result.error or not result.text:
            logger.warning(
                "EOL check: Groq deprecations page unavailable (%s)", result.error,
            )
            return []
        rows = _parse_groq_deprecation_table(result.text)
        if not rows:
            logger.warning(
                "EOL check: Groq deprecations parse yielded 0 rows — page "
                "structure may have changed (this is NOT 'no deprecations')",
            )
        return rows

    async def _surface_eol_observation(
        self, finding: dict, finding_id: str, now: str,
    ) -> str:
        """Surface an ``active_model_eol`` finding as a HIGH-priority recon
        observation (``type='finding'``, ``category='active_model_eol'``).

        Bypasses ``run_intake`` on purpose: curated MODEL_INTELLIGENCE findings
        route to the knowledge base (``intake.route``), where an actionable EOL
        alert would be buried. The dashboard recon tab and the ``recon_findings``
        MCP tool read ``source='recon'`` / ``type='finding'`` observations.
        Deduped on ``(model_id, shutdown_date)`` so the same EOL never re-fires
        while unresolved (self-healing: re-alerts only after resolution).
        """
        import hashlib

        from genesis.db.crud import observations

        model_id = finding.get("model", "")
        shutdown_date = finding.get("shutdown_date", "")
        blast = finding.get("blast_radius", [])
        title = (
            f"Active model EOL: {model_id} (shutdown {shutdown_date}) — "
            f"{len(blast)} chain(s) affected"
        )
        content = json.dumps({"title": title, **finding, "detected_at": now})
        # Stable dedup key — independent of detected_at / blast-radius churn.
        dedup_key = hashlib.sha256(
            json.dumps(
                {"model_id": model_id, "shutdown_date": shutdown_date},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        try:
            await observations.create(
                self._db,
                id=finding_id,
                source="recon",
                type="finding",
                category="active_model_eol",
                content=content,
                priority="high",
                created_at=now,
                content_hash=dedup_key,
                skip_if_duplicate=True,
            )
        except Exception:
            logger.warning(
                "Failed to surface active_model_eol observation for %s",
                model_id, exc_info=True,
            )
        return finding_id
