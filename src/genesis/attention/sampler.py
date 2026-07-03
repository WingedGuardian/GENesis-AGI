"""L1.5 salience sampler — a Genesis-side adapter that asks a cheap FREE LLM to judge a
graduated attention window: is it coherent real speech (``real``), and is it worth a
proactive assistant's attention (``perk``)?

This is the ONLY attention module that makes an LLM call, so it lives OUTSIDE the pure
edge-portable core: it is NOT one of the 6 guarded modules (types/config/triggers/
scorer/engine/clarity) and is NEVER imported from ``attention/__init__`` — importing the
core must stay ``genesis.routing``-free (see ``tests/test_attention/test_edge_portability.py``).

Two invariants:
- **Fail-closed.** Every failure path (route failure, malformed/garbled JSON, missing
  key, non-finite value, any exception) returns ``None``. The shadow run never crashes;
  the verdict simply stays absent (``AttentionEvent.l15_verdict is None``). ``real``/``perk``
  are HARD-required; the v2 ``category``/``reason`` fields are best-effort (a missing or junk
  category/reason never fails the parse, so an old ``{real, perk}`` response still parses).
- **Firewall (v2 — softened 2026-07-02 with explicit user approval).** The window transcript
  TEXT is sent to the LLM in memory only and is NEVER persisted. The stored verdict is the
  judge's OWN output — floats ``{real, perk}`` + a ``category`` enum + a short ``reason`` the
  prompt instructs to be a CHARACTERIZATION, not a verbatim quote. The reason is a judgment
  artifact (Genesis-side, private DB), a deliberate small relaxation of the prior floats-only
  rule so verdicts are auditable; the consumer still persists no raw transcript text.
``sample()`` also stamps ``prompt_version`` (comparability across prompt iterations) and the
serving ``model`` when the router reports it (the chain may fall back off the primary).

Router is INJECTED (``_Router`` Protocol, mirroring ``ego/focus.py``) so the sampler is
unit-testable with a fake and pulls no heavy router import into this module.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Protocol

from genesis.attention.config import AttentionConfig
from genesis.attention.types import AmbientUtterance

logger = logging.getLogger(__name__)

# The router call-site (defined in config/model_routing.yaml). Free-only, cross-vendor
# fallback, non-Groq — see the plan's PR3b bake-off.
CALL_SITE = "attention_salience"

# Stamped into every verdict so a later prompt iteration's verdicts are comparable/filterable
# against these (the analog of a config_version for the judge prompt). Bump on prompt change.
PROMPT_VERSION = "v2"

# The category buckets the judge picks from; anything else the model returns collapses to
# "other" (never fails the parse). "other" is the fallback bucket, not offered in the prompt.
_CATEGORIES = frozenset({"question", "task", "decision", "problem", "chatter", "garble"})

# A hard cap on the stored reason (defence-in-depth on the firewall-soften: a short
# characterization, never a transcript dump). The prompt asks for <=140 chars; we cap at 200.
_REASON_CAP = 200

# ```json ... ``` (or a bare ``` ... ```) fenced block — models love to wrap JSON in one.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class _Router(Protocol):
    """The slice of ``genesis.routing.Router`` the sampler needs (mirrors the injected
    ``_Router`` in ``ego/focus.py:35``). Injecting a Protocol keeps this module free of a
    hard ``Router`` import and makes ``sample`` testable with a fake."""

    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


class AttentionSampler:
    """Scores a graduated attention window via the L1.5 ``attention_salience`` call-site."""

    def __init__(self, router: _Router) -> None:
        self._router = router

    async def sample(
        self, window: list[AmbientUtterance], config: AttentionConfig
    ) -> dict | None:
        """Return the judge verdict dict, or ``None`` on ANY failure.

        On success the dict has ``real``/``perk`` floats in ``[0, 1]`` (always), a
        ``category`` enum + short ``reason`` (best-effort — present when the model supplies
        them), plus provenance this method stamps: ``prompt_version`` (always) and ``model``
        (when the router reports the serving model).

        ``window`` is a FIRE-TIME SNAPSHOT of the recent in-context utterances (frozen,
        immutable ``AmbientUtterance``; the last element is the triggering utt). Its
        ``.text`` is sent to the LLM in memory only — never persisted (the consumer stores
        the returned verdict, no raw transcript). ``config`` is accepted for parity/future
        prompt shaping; the current prompt needs only the window.
        """
        if not window:
            return None
        messages = [{"role": "user", "content": _build_prompt(window)}]
        try:
            result = await self._router.route_call(CALL_SITE, messages)
        except Exception:  # any router/transport fault -> no verdict, never crash the run
            logger.warning("L1.5 sample: route_call raised", exc_info=True)
            return None
        if not getattr(result, "success", False):
            return None
        verdict = _parse_verdict(getattr(result, "content", None))
        if verdict is None:
            return None
        verdict["prompt_version"] = PROMPT_VERSION
        model_id = getattr(result, "model_id", None)
        if model_id:  # RoutingResult.model_id — records WHICH chain model actually judged
            verdict["model"] = model_id
        return verdict


def _build_prompt(window: list[AmbientUtterance]) -> str:
    """A strict-JSON scoring prompt over the ~8s context window (the last line is the
    trigger). Speaker labels give the model turn-taking context; §11 of the design bible
    found the CONTEXT WINDOW (not the bare utterance) is what makes the judgment tractable."""
    lines = []
    for u in window:
        who = "user" if u.is_user == 1 else ("other" if u.is_user == 0 else "?")
        lines.append(f"[{who}] {u.text}")
    transcript = "\n".join(lines)
    return (
        "You judge a short snippet of ambient household speech, auto-transcribed and "
        "possibly garbled. Return a strict JSON object with four fields:\n"
        "- real: float 0.0-1.0 — is this COHERENT real speech (not ASR noise or "
        "hallucination)? 1.0 = clearly real, 0.0 = garble.\n"
        "- perk: float 0.0-1.0 — is this worth a proactive assistant's attention (a "
        "question, task, decision, or problem)? 1.0 = clearly salient, 0.0 = idle chatter.\n"
        "- category: exactly one word from: question, task, decision, problem, chatter, garble.\n"
        "- reason: ONE short sentence (<=140 chars) explaining the scores IN GENERAL TERMS. "
        "Do NOT quote or repeat the transcript verbatim — characterize it (e.g. 'a scheduling "
        "question', not the words spoken).\n\n"
        f"Conversation window (the LAST line is the trigger):\n{transcript}\n\n"
        "Reply with ONLY the JSON object, no prose: "
        '{"real": <float>, "perk": <float>, "category": "<one word>", "reason": "<sentence>"}'
    )


def _first_json_object(text: str) -> str | None:
    """The first brace-balanced ``{...}`` span. A bare ``find``/``rfind`` mis-slices two
    plausible model outputs — an object followed by prose that contains a ``}``, and an
    object with a nested value — so we scan brace depth instead. The scan is STRING-AWARE:
    braces inside a JSON string value are skipped (so a ``}`` in the free-text v2 ``reason``
    doesn't prematurely close the object). A miscount still just fails to parse — fail-closed."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_verdict(content: str | None) -> dict | None:
    """Fail-closed parse of a v2 ``{real, perk, category, reason}`` object from model output.

    Strip a code fence -> take the first brace-balanced ``{...}`` -> ``json.loads``. ``real``
    and ``perk`` are HARD-required: reject booleans (``bool`` subclasses ``int``), coerce to
    float, reject non-finite (NaN/inf), clamp to ``[0, 1]``; any miss -> ``None`` (never
    raises). ``category`` and ``reason`` are BEST-EFFORT and additive — a missing/junk one is
    simply omitted, so an old ``{real, perk}`` response (or a model that ignores the v2 fields)
    still parses cleanly with no invented keys."""
    if not content:
        return None
    text = content.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    span = _first_json_object(text)
    if span is None:
        return None
    try:
        obj = json.loads(span)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    verdict: dict[str, Any] = {}
    for key in ("real", "perk"):
        if key not in obj:
            return None
        raw = obj[key]
        if isinstance(raw, bool):  # bool subclasses int; float(True)==1.0 would slip past
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):  # rejects NaN / inf that json.loads happily parses
            return None
        verdict[key] = min(1.0, max(0.0, value))
    # category (best-effort): a known bucket passes through lowercased; anything the model
    # returns that isn't in the enum collapses to "other"; an absent category is not invented.
    if "category" in obj:
        cat = obj["category"]
        verdict["category"] = (
            cat.strip().lower()
            if isinstance(cat, str) and cat.strip().lower() in _CATEGORIES
            else "other"
        )
    # reason (best-effort): only a non-empty string, stripped and hard-capped; else omitted.
    reason = obj.get("reason")
    if isinstance(reason, str):
        reason = reason.strip()[:_REASON_CAP]
        if reason:
            verdict["reason"] = reason
    return verdict
