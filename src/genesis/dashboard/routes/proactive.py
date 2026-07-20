"""Proactive recall endpoint — the UserPromptSubmit hook's server backend.

``POST /api/genesis/hook/recall`` : ``{prompt, session_id, profile,
file_keywords, suppress_ids}`` in → formatted injection context out (see
:func:`genesis.memory.proactive.proactive_context` for the response shape).

The ``scripts/proactive_memory_hook.py`` thin client calls this over loopback
every prompt and falls back to a degraded FTS5-only local path on any non-200.
Open (no bearer token) — same posture as ``/api/genesis/memory/search`` and
the guardian health probe (user decision 2026-07-19); loopback bind + the
memory system already being reachable via ``/api/genesis/memory/search`` mean
a token would add friction without closing a new surface. Bounded at 2.0s (the
per-prompt latency budget) — on expiry the worker returns 503 and the hook
falls back rather than blocking the prompt.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/hook/recall", methods=["POST"])
@_async_route(timeout=2.0)
async def proactive_recall():
    """Build proactive injection context for one prompt (hook backend)."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    from genesis.memory.proactive import proactive_context, proactive_enabled

    if not proactive_enabled():
        # Engine turned off in config — a CLEAN empty answer (status "disabled"),
        # not an error: the hook injects nothing this turn and does NOT flag
        # degraded (the server is healthy; recall was deliberately silenced).
        return jsonify(
            {
                "status": "disabled",
                "lines": [],
                "results": [],
                "procedure": None,
                "shadow": {},
                "budget": {},
                "embedding": None,
                "timings_ms": {},
                "engine": {},
            }
        )

    def _as_str_list(value: object) -> list[str]:
        return [str(v) for v in value if v] if isinstance(value, list) else []

    try:
        result = await proactive_context(
            prompt=prompt,
            session_id=str(data.get("session_id") or ""),
            profile=str(data.get("profile") or "cc_hook"),
            file_keywords=_as_str_list(data.get("file_keywords")),
            suppress_ids=_as_str_list(data.get("suppress_ids")),
        )
    except RuntimeError:
        # Memory subsystem not initialized yet (partial boot) — 503 so the hook
        # falls back, same class as the not-bootstrapped guard above.
        logger.debug("proactive recall: memory not initialized", exc_info=True)
        return jsonify({"error": "Memory not initialized"}), 503
    except Exception:
        logger.exception("proactive recall failed")
        return jsonify({"error": "recall failed"}), 500

    return jsonify(result)
