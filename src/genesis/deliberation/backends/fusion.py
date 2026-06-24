"""Fusion backend — OpenRouter Fusion (a server-side panel-of-models + judge).

A single OpenAI-compatible call to `openrouter/fusion` runs a panel of expert models
in parallel (with web search) and a judge synthesizes the result. We call litellm
directly (not through the Router) so we can read whatever the response carries and
own cost/error handling — a deliberate, documented exception to the route-everything
convention, justified because deliberate()'s value is the structured synthesis.

Probe-confirmed (live, 2026-06-24): the bare model-slug returns a synthesized answer in
``message.content`` (markdown by default; we system-prompt for JSON), NO side fields; an
explicit ``max_tokens`` is REQUIRED (litellm's 65536 default trips an OpenRouter 402);
the real cost is in ``response.usage.cost`` (``litellm.completion_cost`` can't price it);
latency is ~80s.
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

# litellm model string = openrouter prefix + the `openrouter/fusion` model id (probe-confirmed;
# the bare `openrouter/fusion` 502s).
_MODEL = "openrouter/openrouter/fusion"
_MAX_TOKENS = 2000  # explicit — the 65536 default trips OpenRouter's "fewer max_tokens" 402.
_DEFAULT_TIMEOUT_S = 180.0  # panel + web search runs ~80s; leave generous headroom.
_ANSWER_CAP = 6000

_SYSTEM = (
    "You are a panel of independent expert models with a judge. Deliberate on the user's "
    "question with genuine rigor. Return ONLY a JSON object (no markdown fences, no prose "
    "outside the JSON) with keys: "
    '"answer" (string — the recommendation/verdict), '
    '"consensus" (string — what the panel broadly agrees on), '
    '"dissent" (array of strings — genuine minority or contrarian points; [] only if the panel '
    "truly agrees), "
    '"confidence" (number between 0 and 1). '
    "Surface real disagreement — never manufacture false consensus, never suppress a minority view."
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
    name = "fusion"

    async def run(
        self,
        question: str,
        *,
        context: str | None = None,
        stakes: str = "normal",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        models: list[str] | None = None,  # noqa: ARG002 — Fusion's panel is server-side (plugin-only)
    ) -> DeliberationResult:
        key = _openrouter_key()
        if not key:
            return DeliberationResult(
                answer=None, error="OpenRouter API key not configured (API_KEY_OPENROUTER)"
            )
        system = _SYSTEM + (_HIGH_SUFFIX if stakes == "high" else "")
        user = question if not context else f"{question}\n\nContext:\n{context}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=_MODEL,
                    messages=messages,
                    api_key=key,
                    max_tokens=_MAX_TOKENS,
                    drop_params=True,
                    num_retries=0,
                    timeout=timeout_s,
                ),
                timeout=timeout_s + 10,
            )
        except TimeoutError:
            return DeliberationResult(
                answer=None,
                latency_s=time.perf_counter() - t0,
                error=f"fusion timed out after ~{timeout_s:.0f}s",
            )
        except Exception as exc:  # noqa: BLE001 — map ALL litellm errors to a graceful result
            return _error_result(exc, time.perf_counter() - t0)
        return _normalize(resp, time.perf_counter() - t0)


def _error_result(exc: Exception, latency: float) -> DeliberationResult:
    status = getattr(exc, "status_code", None)
    return DeliberationResult(
        answer=None,
        latency_s=latency,
        error=f"fusion call failed ({type(exc).__name__}, status={status}): {str(exc)[:300]}",
    )


def _normalize(resp: object, latency: float) -> DeliberationResult:
    content = _content(resp)
    parsed = _parse_content(content)
    cost, cost_known = _extract_cost(resp)
    answer = parsed.get("answer") or (content[:_ANSWER_CAP] if content else None)
    return DeliberationResult(
        answer=answer,
        consensus=parsed.get("consensus"),
        dissent=tuple(parsed.get("dissent") or ()),
        confidence=parsed.get("confidence"),
        per_model=(),
        backend_used="fusion",
        latency_s=latency,
        cost_usd=cost,
        cost_known=cost_known,
        error=None if content else "fusion returned empty content",
    )


def _content(resp: object) -> str:
    try:
        return resp.choices[0].message.content or ""  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return ""


def _parse_content(content: str) -> dict:
    """Dual-path: structured JSON if Fusion complied with the system prompt, else {} so the
    caller falls back to the raw content as ``answer`` (still a rich verdict)."""
    if not content:
        return {}
    candidates = [content]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
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
    dissent = obj.get("dissent")
    if isinstance(dissent, list):
        out["dissent"] = [str(x) for x in dissent if x]
    elif isinstance(dissent, str) and dissent.strip():
        out["dissent"] = [dissent.strip()]
    conf = obj.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = max(0.0, min(1.0, float(conf)))
    return out


def _extract_cost(resp: object) -> tuple[float, bool]:
    """Cost lives in response.usage.cost (probe-confirmed); litellm.completion_cost can't price fusion."""
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
