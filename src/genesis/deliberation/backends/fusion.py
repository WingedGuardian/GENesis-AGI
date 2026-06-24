"""Fusion backend — OpenRouter Fusion (a server-side panel-of-models + judge).

Two modes × two presets, all probe-confirmed live (2026-06-24):

- **synthesis** (default, fast): the `openrouter/fusion` model-slug → a synthesized prose verdict in
  ``message.content`` (the meta-model IGNORES output-format prompts, so we take its markdown as the
  ``answer``; consensus/dissent stay empty). Default preset: **budget**.
- **analysis** (deep): a free, tool-capable orchestrator + the Fusion *server-tool*
  (`tools:[{"type":"openrouter:fusion"}]`, `tool_choice:"required"`, via extra_body) → the orchestrator
  (a normal instruct model that honors a JSON system prompt) returns machine-structured
  {answer, consensus, dissent[], blind_spots[], confidence}. Default preset: **strong**.

Presets control the panel (who deliberates) — explicit, custom-defined model lists (see _PRESET_PANELS):
**strong** = a frontier panel (opus/gpt/gemini-pro/grok/deepseek/kimi) judged by gpt; **budget** = a
mid-tier panel (deepseek/gpt-mini/grok/qwen/kimi/gemini-flash) judged by sonnet (cheaper/faster). Passed
as `analysis_models` (panel) + `model` (judge) via the fusion `plugins` entry (synthesis) or the
server-tool `parameters` (analysis). Edit _PRESET_PANELS to recompose the chorus.

We POST the OpenRouter chat-completions endpoint over a raw streaming ``httpx`` connection (NOT via
litellm / the Router) so we own the request shape AND can read the streamed ``usage.cost``. We must
STREAM: Fusion convenes a server-side panel that can run for minutes, and OpenRouter pads that wait with
SSE keep-alive comments (lines starting ``:``) that break a non-streaming ``.json()`` parse (observed:
"Unable to get json response" on a 6-model panel). litellm's streaming path survives the padding but
STRIPS the provider ``cost`` from its Usage object and hides OpenRouter's generation id, so neither
litellm path can report cost — hence the direct SSE read. An explicit ``max_tokens`` is REQUIRED
(OpenRouter's large default trips a "fewer max_tokens" 402); cost comes from the final ``usage.cost``
SSE chunk — the only place these dynamic-priced aggregator calls expose it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

from genesis.deliberation.types import DeliberationResult

logger = logging.getLogger(__name__)

_OR_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# synthesis: the `openrouter/fusion` model-slug — the meta-model itself (raw OpenRouter id, probe-confirmed).
_MODEL = "openrouter/fusion"
# analysis: a free, tool-capable orchestrator that calls the fusion server-tool and returns structured JSON.
_ANALYSIS_ORCHESTRATOR = "openai/gpt-oss-120b:free"

# Explicit chorus panels (`analysis_models`) + judge (`model`) per preset. EDIT THESE to change the
# chorus. `~…-latest` ids float to the newest version. Panel/judge models need NOT support tools.
_PRESET_PANELS: dict[str, dict] = {
    "strong": {
        "analysis_models": [
            "~anthropic/claude-opus-latest",
            "~openai/gpt-latest",
            "~google/gemini-pro-latest",
            "x-ai/grok-4.3",
            "deepseek/deepseek-v4-pro",
            "~moonshotai/kimi-latest",
        ],
        "model": "~openai/gpt-latest",  # judge
    },
    "budget": {
        "analysis_models": [
            "deepseek/deepseek-v4-pro",
            "~openai/gpt-mini-latest",
            "x-ai/grok-4.3",
            "qwen/qwen3-235b-a22b-thinking-2507",
            "~moonshotai/kimi-latest",
            "~google/gemini-flash-latest",
        ],
        "model": "~anthropic/claude-sonnet-latest",  # judge
    },
}
# synthesis defaults to budget; analysis is ALWAYS strong (pinned in _resolve_preset).
_DEFAULT_PRESET = {"synthesis": "budget", "analysis": "strong"}

_MAX_TOKENS = 2000  # explicit — the 65536 default trips OpenRouter's "fewer max_tokens" 402.
_DEFAULT_TIMEOUT_S = 240.0  # analysis (panel + judge + orchestrator) runs ~120-170s; synthesis ~47-110s.
_ANSWER_CAP = 6000
# deliberate() owns its own retry on TRANSIENT errors (frontier panels 429 under load — observed on the
# strong preset). Retried: 429/5xx + transport errors. NOT retried: other 4xx and timeouts.
_MAX_ATTEMPTS = 3
_BACKOFF_S = 3.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

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


def _resolve_preset(preset: str | None, mode: str) -> tuple[str, dict]:
    """(preset_name, panel_config). Analysis is ALWAYS strong; synthesis defaults to budget."""
    if mode == "analysis":
        return "strong", _PRESET_PANELS["strong"]
    name = preset if preset in _PRESET_PANELS else _DEFAULT_PRESET.get(mode, "budget")
    return name, _PRESET_PANELS[name]


def _resolve_stakes(stakes: str | None, mode: str, preset_name: str) -> str:
    """Auto-couple stakes when not explicitly given: analysis→high, synthesis strong→high, budget→normal.
    An explicit "normal"/"high" always wins."""
    if stakes in ("normal", "high"):
        return stakes
    return "high" if (mode == "analysis" or preset_name == "strong") else "normal"


class FusionBackend:
    """OpenRouter Fusion backend — `synthesis` (model-slug) and `analysis` (server-tool) modes."""

    name = "fusion"

    async def run(
        self,
        question: str,
        *,
        context: str | None = None,
        stakes: str | None = None,
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
        preset_name, panel = _resolve_preset(preset, mode)
        high = _HIGH_SUFFIX if _resolve_stakes(stakes, mode, preset_name) == "high" else ""
        fusion_cfg = {"analysis_models": panel["analysis_models"], "model": panel["model"]}
        if mode == "analysis":
            extra = {
                "tools": [{"type": "openrouter:fusion", "parameters": fusion_cfg}],
                "tool_choice": "required",
            }
            return await _call(
                _ANALYSIS_ORCHESTRATOR, _ANALYSIS_SYSTEM + high, user, key, timeout_s, extra,
                "analysis", preset_name,
            )
        extra = {"plugins": [{"id": "fusion", **fusion_cfg}]}
        return await _call(_MODEL, _SYNTHESIS_SYSTEM + high, user, key, timeout_s, extra, "synthesis", preset_name)


async def _call(model, system, user, key, timeout_s, extra_body, mode, preset) -> DeliberationResult:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": _MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},  # final SSE chunk carries usage.cost
    }
    if extra_body:
        body.update(extra_body)
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            content, cost, stream_err = await asyncio.wait_for(
                _consume_stream(body, key, timeout_s), timeout=timeout_s + 10
            )
        except (TimeoutError, httpx.TimeoutException):
            return DeliberationResult(
                answer=None,
                backend_used=f"fusion/{mode}",
                preset_used=preset,
                latency_s=time.perf_counter() - t0,
                error=f"fusion ({mode}) timed out after ~{timeout_s:.0f}s",
            )
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code in _RETRYABLE_STATUS and attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_S * (attempt + 1))
                continue
            return _error_result(exc, time.perf_counter() - t0, mode, preset)
        except httpx.TransportError as exc:
            # connection reset / read error mid-panel — transient, retry
            last_exc = exc
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_S * (attempt + 1))
                continue
            return _error_result(exc, time.perf_counter() - t0, mode, preset)
        except Exception as exc:  # noqa: BLE001 — any other error → graceful result, never raise
            return _error_result(exc, time.perf_counter() - t0, mode, preset)
        else:
            if not content and attempt + 1 < _MAX_ATTEMPTS:
                # transient empty / in-stream panel error from a flaky panel — one more shot
                last_exc = RuntimeError(stream_err or "empty stream")
                await asyncio.sleep(_BACKOFF_S * (attempt + 1))
                continue
            return _normalize(content, cost, stream_err, time.perf_counter() - t0, mode, preset)
    return _error_result(last_exc or RuntimeError("retries exhausted"), time.perf_counter() - t0, mode, preset)


async def _consume_stream(body: dict, key: str, timeout_s: float) -> tuple[str, float | None, str | None]:
    """Raw streaming POST → buffer the SSE lines, then parse content + final usage.cost + any error.

    Wrapped in asyncio.wait_for by the caller. Raises httpx errors (status / transport / timeout) for the
    caller's retry map; ``raise_for_status`` turns a 4xx/5xx into ``httpx.HTTPStatusError``.
    """
    headers = {"Authorization": f"Bearer {key}"}  # httpx sets Content-Type from json=
    timeout = httpx.Timeout(timeout_s, connect=30.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("POST", _OR_ENDPOINT, json=body, headers=headers) as resp,
    ):
        if resp.status_code >= 400:
            await resp.aread()  # read the error body so HTTPStatusError carries a message
            resp.raise_for_status()
        lines = [line async for line in resp.aiter_lines()]
    return _parse_sse(lines)


def _parse_sse(lines: list[str]) -> tuple[str, float | None, str | None]:
    """Parse OpenRouter SSE lines: skip ``:`` keep-alive comments, assemble ``delta.content``, read the
    final ``usage.cost``, and capture any in-stream ``error`` message (OpenRouter reports mid-stream
    panel failures as an ``error`` object on a 200 response). Tolerant of malformed / partial lines."""
    parts: list[str] = []
    cost: float | None = None
    error: str | None = None
    for line in lines:
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        for ch in obj.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content") if isinstance(ch, dict) else None
            if piece:
                parts.append(piece)
        usage = obj.get("usage")
        if isinstance(usage, dict):
            c = usage.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                cost = float(c)
        err = obj.get("error")
        if err:
            error = err.get("message") if isinstance(err, dict) else str(err)
    return "".join(parts), cost, error


def _error_result(exc: Exception, latency: float, mode: str, preset: str) -> DeliberationResult:
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
    return DeliberationResult(
        answer=None,
        backend_used=f"fusion/{mode}",
        preset_used=preset,
        latency_s=latency,
        error=f"fusion ({mode}) call failed ({type(exc).__name__}, status={status}): {str(exc)[:300]}",
    )


def _normalize(
    content: str, cost: float | None, error: str | None, latency: float, mode: str, preset: str
) -> DeliberationResult:
    parsed = _parse_content(content)
    cost_known = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    answer = parsed.get("answer") or (content[:_ANSWER_CAP] if content else None)
    err = None
    if not content:  # surface the real in-stream reason when we got nothing back
        err = f"fusion ({mode}) returned no content"
        if error:
            err += f": {error[:200]}"
    elif error:
        # content arrived AND a late in-stream error — the verdict is usable but may be partial;
        # don't false-fail a complete answer, but don't drop the signal either (log it).
        logger.warning("fusion (%s) delivered content with a trailing in-stream error: %s", mode, error[:200])
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
        cost_usd=float(cost) if cost_known else 0.0,
        cost_known=cost_known,
        error=err,
    )


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
