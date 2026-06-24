"""Fusion backend — OpenRouter Fusion (a server-side panel-of-models + judge).

Two modes, both probe-confirmed live (2026-06-24):

- **synthesis** (default, fast ~47s, ~$0.16): the `openrouter/fusion` model-slug. A meta-model
  synthesizes the panel into a prose verdict in ``message.content``. It IGNORES output-format
  instructions, so we take its markdown as the ``answer`` (consensus/dissent stay empty — the
  disagreement is woven into the prose).
- **analysis** (deep ~170s, ~$0.12): a free, tool-capable orchestrator + the Fusion *server-tool*
  (`tools:[{"type":"openrouter:fusion"}]`, `tool_choice:"required"`, passed via extra_body so litellm
  doesn't drop the non-standard tool). The orchestrator runs the panel, then — because it IS a normal
  instruct model that honors a system prompt — returns machine-structured JSON
  {answer, consensus, dissent[], blind_spots[], confidence}.

We call litellm directly (not the Router) to own the request shape, cost, and error handling — a
documented exception to route-everything, justified by deliberate()'s structured-synthesis value.
An explicit ``max_tokens`` is REQUIRED (litellm's 65536 default trips an OpenRouter 402); cost is read
from ``response.usage.cost`` (``litellm.completion_cost`` can't price these aggregator strings).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import litellm

from genesis.deliberation.types import DeliberationResult

logger = logging.getLogger(__name__)

# synthesis: openrouter prefix + the `openrouter/fusion` model id (probe-confirmed; bare `openrouter/fusion` 502s).
_MODEL = "openrouter/openrouter/fusion"
# analysis: a free, tool-capable orchestrator that calls the fusion server-tool and returns structured JSON.
_ANALYSIS_ORCHESTRATOR = "openrouter/openai/gpt-oss-120b:free"
_ANALYSIS_EXTRA = {"tools": [{"type": "openrouter:fusion"}], "tool_choice": "required"}

_MAX_TOKENS = 2000  # explicit — the 65536 default trips OpenRouter's "fewer max_tokens" 402.
_DEFAULT_TIMEOUT_S = 240.0  # analysis (panel + judge + orchestrator) runs ~170s; synthesis ~47s.
_ANSWER_CAP = 6000

_SYNTHESIS_SYSTEM = (
    "You are a panel of independent expert models with a judge. Deliberate on the user's question "
    "with genuine rigor. If you can, return a JSON object with keys answer, consensus, dissent (array), "
    "confidence (0..1); otherwise give a clear verdict that explicitly surfaces where the panel "
    "disagrees. Never manufacture false consensus; never suppress a minority view."
)
_ANALYSIS_SYSTEM = (
    "You have a fusion tool that convenes a panel of expert models and a judge. Call it, then return "
    "ONLY a JSON object (no prose outside the JSON) with keys: "
    '"answer" (string verdict), "consensus" (string — what the panel agreed on), '
    '"dissent" (array of strings — genuine minority/contrarian points from the panel), '
    '"blind_spots" (array of strings — what no panel model addressed), '
    '"confidence" (number 0..1). Surface real disagreement; never invent false consensus.'
)
_HIGH_SUFFIX = (
    " This is a HIGH-STAKES decision: weight dissent heavily and flag even a single-model objection."
)


def _openrouter_key() -> str | None:
    for var in ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
        val = os.environ.get(var)
        if val and val not in ("None", "NA", ""):
            return val
    return None


class FusionBackend:
    """OpenRouter Fusion backend — `synthesis` (model-slug) and `analysis` (server-tool) modes."""

    name = "fusion"

    async def run(
        self,
        question: str,
        *,
        context: str | None = None,
        stakes: str = "normal",
        mode: str = "synthesis",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        models: list[str] | None = None,  # noqa: ARG002 — Fusion's panel is server-side
    ) -> DeliberationResult:
        """Deliberate via Fusion in the given mode. Returns a DeliberationResult; never raises."""
        key = _openrouter_key()
        if not key:
            return DeliberationResult(
                answer=None, error="OpenRouter API key not configured (API_KEY_OPENROUTER)"
            )
        user = question if not context else f"{question}\n\nContext:\n{context}"
        high = _HIGH_SUFFIX if stakes == "high" else ""
        if mode == "analysis":
            return await _call(
                _ANALYSIS_ORCHESTRATOR, _ANALYSIS_SYSTEM + high, user, key, timeout_s, _ANALYSIS_EXTRA, "analysis"
            )
        return await _call(_MODEL, _SYNTHESIS_SYSTEM + high, user, key, timeout_s, None, "synthesis")


async def _call(model, system, user, key, timeout_s, extra_body, mode) -> DeliberationResult:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    kwargs = {
        "model": model,
        "messages": messages,
        "api_key": key,
        "max_tokens": _MAX_TOKENS,
        "drop_params": True,
        "num_retries": 0,
        "timeout": timeout_s,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout_s + 10)
    except (TimeoutError, litellm.Timeout):
        return DeliberationResult(
            answer=None,
            backend_used=f"fusion/{mode}",
            latency_s=time.perf_counter() - t0,
            error=f"fusion ({mode}) timed out after ~{timeout_s:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001 — map ALL litellm errors to a graceful result
        return _error_result(exc, time.perf_counter() - t0, mode)
    return _normalize(resp, time.perf_counter() - t0, mode)


def _error_result(exc: Exception, latency: float, mode: str) -> DeliberationResult:
    status = getattr(exc, "status_code", None)
    return DeliberationResult(
        answer=None,
        backend_used=f"fusion/{mode}",
        latency_s=latency,
        error=f"fusion ({mode}) call failed ({type(exc).__name__}, status={status}): {str(exc)[:300]}",
    )


def _normalize(resp: object, latency: float, mode: str) -> DeliberationResult:
    content = _content(resp)
    parsed = _parse_content(content)
    cost, cost_known = _extract_cost(resp)
    answer = parsed.get("answer") or (content[:_ANSWER_CAP] if content else None)
    return DeliberationResult(
        answer=answer,
        consensus=parsed.get("consensus"),
        dissent=tuple(parsed.get("dissent") or ()),
        blind_spots=tuple(parsed.get("blind_spots") or ()),
        confidence=parsed.get("confidence"),
        per_model=(),
        backend_used=f"fusion/{mode}",
        latency_s=latency,
        cost_usd=cost,
        cost_known=cost_known,
        error=None if content else f"fusion ({mode}) returned empty content",
    )


def _content(resp: object) -> str:
    try:
        return resp.choices[0].message.content or ""  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return ""


def _parse_content(content: str) -> dict:
    """Dual-path: structured JSON when the model emits it (analysis mode, or a compliant synthesis),
    else {} so the caller uses the raw content as ``answer`` (synthesis prose)."""
    if not content:
        return {}
    candidates = [content]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    braced = re.search(r"\{.*\}", content, re.DOTALL)
    if braced:
        candidates.append(braced.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and ("answer" in obj or "consensus" in obj):
            return _coerce(obj)
    return {}


def _coerce(obj: dict) -> dict:
    out: dict = {}
    if isinstance(obj.get("answer"), str):
        out["answer"] = obj["answer"]
    if isinstance(obj.get("consensus"), str):
        out["consensus"] = obj["consensus"]
    for key in ("dissent", "blind_spots"):
        val = obj.get(key)
        if isinstance(val, list):
            out[key] = [str(x) for x in val if x]
        elif isinstance(val, str) and val.strip():
            out[key] = [val.strip()]
    conf = obj.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = max(0.0, min(1.0, float(conf)))
    return out


def _extract_cost(resp: object) -> tuple[float, bool]:
    """Cost is in response.usage.cost (probe-confirmed); litellm.completion_cost can't price fusion."""
    usage = getattr(resp, "usage", None)
    if usage is not None:
        cost = getattr(usage, "cost", None)
        if cost is None and isinstance(usage, dict):
            cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            return float(cost), True
    hidden = getattr(resp, "_hidden_params", {}) or {}
    rc = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if isinstance(rc, (int, float)) and not isinstance(rc, bool):
        return float(rc), True
    return 0.0, False
