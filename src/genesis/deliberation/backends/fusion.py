"""Fusion backend — OpenRouter Fusion (a server-side panel-of-models + judge).

Two modes × two presets, all probe-confirmed live (2026-06-24):

- **synthesis** (default, fast): the `openrouter/fusion` model-slug → a synthesized prose verdict in
  ``message.content`` (the meta-model IGNORES output-format prompts, so we take its markdown as the
  ``answer``; consensus/dissent stay empty). Default preset: **budget**.
- **analysis** (deep): a free, tool-capable orchestrator + the Fusion *server-tool*
  (`tools:[{"type":"openrouter:fusion"}]`, `tool_choice:"required"`, via extra_body) → the orchestrator
  (a normal instruct model that honors a JSON system prompt) returns machine-structured
  {answer, consensus, dissent[], blind_spots[], confidence}. Default preset: **strong**.

Presets control the panel (who deliberates): **strong** → OpenRouter's `general-high` (strongest panel);
**budget** → `general-budget` (mid-tier panel, cheaper). Passed via the fusion `plugins` entry
(synthesis) or the server-tool `parameters` (analysis). Verified: budget → sonnet-3.5/gpt-4o/gemini-1.5.

We call `litellm.acompletion` directly (a documented exception to routing via the Router) to own the
request shape and read side data: an explicit ``max_tokens`` is REQUIRED (litellm's 65536 default trips
an OpenRouter 402); cost is read from ``response.usage.cost`` (``litellm.completion_cost`` can't price
these aggregator strings).
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

# Our two presets → OpenRouter's curated panel+judge slugs.
_PRESET_SLUG = {"strong": "general-high", "budget": "general-budget"}
# Mode-aware default when the caller doesn't specify a preset.
_DEFAULT_PRESET = {"synthesis": "budget", "analysis": "strong"}

_MAX_TOKENS = 2000  # explicit — the 65536 default trips OpenRouter's "fewer max_tokens" 402.
_DEFAULT_TIMEOUT_S = 240.0  # analysis (panel + judge + orchestrator) runs ~120-170s; synthesis ~47-110s.
_ANSWER_CAP = 6000
# deliberate() bypasses the Router, so it owns its own retry on TRANSIENT provider errors (frontier
# panels 429 under load — observed on the strong/general-high preset). 4xx and timeouts are NOT retried.
_MAX_ATTEMPTS = 3
_BACKOFF_S = 3.0

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


def _resolve_preset(preset: str | None, mode: str) -> tuple[str, str]:
    """(preset_name, openrouter_slug). Falls back to the mode-aware default for unknown/None."""
    name = preset if preset in _PRESET_SLUG else _DEFAULT_PRESET.get(mode, "budget")
    return name, _PRESET_SLUG[name]


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
        preset: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        models: list[str] | None = None,  # noqa: ARG002 — panel is set via preset, not a client list
    ) -> DeliberationResult:
        """Deliberate via Fusion in the given mode/preset. Returns a DeliberationResult; never raises."""
        key = _openrouter_key()
        if not key:
            return DeliberationResult(
                answer=None, error="OpenRouter API key not configured (API_KEY_OPENROUTER)"
            )
        user = question if not context else f"{question}\n\nContext:\n{context}"
        high = _HIGH_SUFFIX if stakes == "high" else ""
        preset_name, slug = _resolve_preset(preset, mode)
        if mode == "analysis":
            extra = {
                "tools": [{"type": "openrouter:fusion", "parameters": {"preset": slug}}],
                "tool_choice": "required",
            }
            return await _call(
                _ANALYSIS_ORCHESTRATOR, _ANALYSIS_SYSTEM + high, user, key, timeout_s, extra,
                "analysis", preset_name,
            )
        extra = {"plugins": [{"id": "fusion", "preset": slug}]}
        return await _call(_MODEL, _SYNTHESIS_SYSTEM + high, user, key, timeout_s, extra, "synthesis", preset_name)


async def _call(model, system, user, key, timeout_s, extra_body, mode, preset) -> DeliberationResult:
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
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout_s + 10)
        except (TimeoutError, litellm.Timeout):
            return DeliberationResult(
                answer=None,
                backend_used=f"fusion/{mode}",
                preset_used=preset,
                latency_s=time.perf_counter() - t0,
                error=f"fusion ({mode}) timed out after ~{timeout_s:.0f}s",
            )
        except (litellm.RateLimitError, litellm.ServiceUnavailableError) as exc:
            # transient — back off and retry (frontier panels 429 under load)
            last_exc = exc
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_S * (attempt + 1))
                continue
            return _error_result(exc, time.perf_counter() - t0, mode, preset)
        except Exception as exc:  # noqa: BLE001 — map ALL other litellm errors to a graceful result
            return _error_result(exc, time.perf_counter() - t0, mode, preset)
        else:
            return _normalize(resp, time.perf_counter() - t0, mode, preset)
    return _error_result(last_exc or RuntimeError("retries exhausted"), time.perf_counter() - t0, mode, preset)


def _error_result(exc: Exception, latency: float, mode: str, preset: str) -> DeliberationResult:
    status = getattr(exc, "status_code", None)
    return DeliberationResult(
        answer=None,
        backend_used=f"fusion/{mode}",
        preset_used=preset,
        latency_s=latency,
        error=f"fusion ({mode}) call failed ({type(exc).__name__}, status={status}): {str(exc)[:300]}",
    )


def _normalize(resp: object, latency: float, mode: str, preset: str) -> DeliberationResult:
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
        preset_used=preset,
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
