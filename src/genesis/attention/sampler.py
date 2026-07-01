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
  the verdict simply stays absent (``AttentionEvent.l15_verdict is None``).
- **Firewall.** The window transcript TEXT is sent to the LLM in memory only. The stored
  verdict is floats-only (``{real, perk}``); the consumer persists no text (``consumers``).

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
        """Return ``{"real": float, "perk": float}`` in ``[0, 1]``, or ``None`` on ANY failure.

        ``window`` is a FIRE-TIME SNAPSHOT of the recent in-context utterances (frozen,
        immutable ``AmbientUtterance``; the last element is the triggering utt). Its
        ``.text`` is sent to the LLM in memory only — never persisted (the consumer stores
        the returned floats, no text). ``config`` is accepted for parity/future prompt
        shaping; the current prompt needs only the window.
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
        return _parse_verdict(getattr(result, "content", None))


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
        "possibly garbled. Score two things, each a float from 0.0 to 1.0:\n"
        "- real: is this COHERENT real speech (not ASR noise or hallucination)? "
        "1.0 = clearly real, 0.0 = garble.\n"
        "- perk: is this worth a proactive assistant's attention — a question, task, "
        "decision, or problem? 1.0 = clearly salient, 0.0 = idle chatter.\n\n"
        f"Conversation window (the LAST line is the trigger):\n{transcript}\n\n"
        'Reply with ONLY a JSON object, no prose: {"real": <float>, "perk": <float>}'
    )


def _first_json_object(text: str) -> str | None:
    """The first brace-balanced ``{...}`` span. A bare ``find``/``rfind`` mis-slices two
    plausible model outputs — an object followed by prose that contains a ``}``, and an
    object with a nested value — so we scan brace depth instead. (Braces inside string
    values aren't tracked; irrelevant for a numeric ``{real, perk}`` object, and a miscount
    just fails to parse — fail-closed.)"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_verdict(content: str | None) -> dict | None:
    """Fail-closed parse of a ``{real, perk}`` object from model output.

    Strip a code fence -> take the first brace-balanced ``{...}`` -> ``json.loads`` ->
    require BOTH keys, reject booleans (``bool`` subclasses ``int``), coerce to float,
    reject non-finite (NaN/inf), clamp to ``[0, 1]``. Any miss -> ``None`` (never raises)."""
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
    verdict: dict[str, float] = {}
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
    return verdict
