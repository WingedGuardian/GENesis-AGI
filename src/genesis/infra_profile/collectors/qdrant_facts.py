"""Qdrant configuration facts.

``GET /collections`` returns names only, so config comes from a per-collection
``GET /collections/{name}``. Collection config (vector size, distance, HNSW,
quantization, on_disk) is facts; point counts are metrics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from genesis.env import qdrant_url
from genesis.infra_profile.types import SectionResult

logger = logging.getLogger(__name__)

# Failure mode: Qdrant wedged-but-listening (seen during snapshot restores)
# would otherwise hold the refresh flock indefinitely from a boot-path task.
# Healthy localhost responses are single-digit ms; 5s per request tolerates
# heavy load. Requests are per-collection (2 today), so worst case ~15s
# before the section degrades to error — the section, never the refresh.
_HTTP_TIMEOUT = 5.0


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 — localhost service URL from config
        return json.loads(resp.read().decode())


def _collect_sync(base_url: str) -> SectionResult:
    facts: dict = {"url": base_url}
    metrics: dict = {}

    listing = _get_json(f"{base_url}/collections")
    names = sorted(c.get("name", "") for c in listing.get("result", {}).get("collections", []))

    collections: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for name in names:
        detail = _get_json(f"{base_url}/collections/{name}").get("result", {})
        config = detail.get("config", {})
        params = config.get("params", {})
        collections[name] = {
            "vectors": params.get("vectors"),
            "on_disk_payload": params.get("on_disk_payload"),
            "hnsw": config.get("hnsw_config"),
            "quantization": config.get("quantization_config"),
        }
        counts[name] = detail.get("points_count")
    facts["collections"] = collections
    metrics["points_count"] = counts

    return SectionResult(name="qdrant", facts=facts, metrics=metrics)


async def collect_qdrant(base_url: str | None = None) -> SectionResult:
    url = (base_url or qdrant_url()).rstrip("/")
    try:
        return await asyncio.to_thread(_collect_sync, url)
    except Exception as exc:  # urllib raises a small zoo; degrade uniformly
        return SectionResult.failed("qdrant", f"qdrant unreachable: {exc}")
