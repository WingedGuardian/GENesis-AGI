"""Model Intelligence Job — scan model landscape, detect changes.

Runs weekly (Sundays 6am, per config/recon_schedules.yaml) or on-demand
via recon MCP tool. Checks OpenRouter for new/changed models, flags stale
profiles, and stores findings for strategic reflection review.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import httpx

if TYPE_CHECKING:
    from genesis.routing.model_profiles import ModelProfileRegistry

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class ModelIntelligenceJob:
    """Scans the model landscape and compares against known profiles."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        profile_registry: ModelProfileRegistry | None = None,
        profiles_path: Path | None = None,
    ):
        self._db = db
        self._registry = profile_registry
        self._profiles_path = profiles_path

    async def run(self) -> dict:
        """Run the full model intelligence scan.

        Returns summary dict with findings count and categories.
        """
        findings: list[dict] = []

        # 1. OpenRouter model check
        or_findings = await self._check_openrouter()
        findings.extend(or_findings)

        # 2. Profile staleness check
        stale_findings = await self._check_staleness()
        findings.extend(stale_findings)

        # 3. Store all findings
        for f in findings:
            await self._store_finding(f)

        logger.info(
            "Model intelligence scan complete: %d findings", len(findings),
        )
        return {
            "total_findings": len(findings),
            "openrouter_findings": len(or_findings),
            "stale_findings": len(stale_findings),
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
                # Potentially new model — only flag high-context or from known providers
                context = model.get("context_length", 0)
                if context >= 100_000:
                    findings.append({
                        "type": "new_model",
                        "api_id": model_id,
                        "name": model.get("name", model_id),
                        "context_length": context,
                        "pricing": model.get("pricing", {}),
                    })

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
        """Store a model intelligence finding in observations."""
        import uuid

        finding_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        finding_type = finding.get("type", "unknown")

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

        priority = "medium" if finding_type in ("pricing_change", "new_model") else "low"

        await self._db.execute(
            "INSERT INTO observations (id, source, type, category, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (finding_id, "recon", "finding", "model_intelligence", content, priority, now),
        )
        await self._db.commit()
        return finding_id
