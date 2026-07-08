"""Shared in-process implementations for the two synchronous outreach RPCs.

`outreach_send_and_wait` and `provision_grow` block on a live user reply, so they
can only run where the real `OutreachPipeline` (and its single-owner Telegram
reply-waiter) lives — the genesis-server process. These helpers hold that logic
ONCE; two entry points call them with the same live pipeline:

* the `@mcp.tool()` wrappers in ``genesis.mcp.outreach_mcp`` — used when the MCP
  module is initialized in-process with a real pipeline (i.e. inside the server);
* the dashboard HTTP routes (``routes/outreach.py``, ``routes/provision.py``) —
  the bridge the standalone MCP subprocess POSTs to, since its own pipeline is
  ``None``.

Keeping one implementation avoids the drift a duplicated provision body would
invite. Both return plain dicts; callers serialize (``json.dumps`` / ``jsonify``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genesis.outreach.types import OutreachCategory, OutreachRequest

if TYPE_CHECKING:
    from genesis.outreach.pipeline import OutreachPipeline


async def send_and_wait_via_pipeline(
    pipeline: OutreachPipeline,
    *,
    message: str,
    category: str,
    channel: str,
    timeout_s: float,
) -> dict:
    """Send a message and block for the owner's reply. Returns a JSON-able dict."""
    try:
        cat = OutreachCategory(category)
    except ValueError:
        return {"error": f"invalid category '{category}'"}

    req = OutreachRequest(
        category=cat,
        topic=message[:100],
        context=message,
        salience_score=1.0,
        signal_type=category,
        channel=channel,
    )
    result, reply = await pipeline.submit_and_wait(req, timeout_s=float(timeout_s))
    return {
        "outreach_id": result.outreach_id,
        "status": result.status.value,
        "reply": reply,
        "timed_out": reply is None and result.status.value == "delivered",
    }


async def grow_via_pipeline(
    pipeline: OutreachPipeline,
    *,
    kind: str,
    disk: str,
    gib: int,
    mib: int,
    timeout_s: float,
) -> dict:
    """Approval-gated disk/RAM grow: ask the owner, and on APPROVE run the host
    guardian execute verb (which re-checks the due-diligence gate). Returns the
    coordinator's result dict; never mutates without an APPROVE reply.
    """
    # Function-local: keep the guardian/observability trees out of the eager
    # dashboard-route import path (routes/__init__ imports every module at
    # blueprint registration) — matches the convention in outreach_mcp.py.
    from genesis.guardian.provisioning.container import (
        coordinate_grow_disk,
        coordinate_grow_memory,
    )
    from genesis.observability.health import _load_guardian_remote_from_config

    remote = _load_guardian_remote_from_config()
    if remote is None:
        return {"ok": False, "error": "guardian remote not configured (no guardian_remote.yaml)"}

    async def _ask(text: str) -> str | None:
        # submit_RAW_and_wait: deliver the proposal VERBATIM (skip the LLM
        # drafter). The proposal ends in an exact "reply APPROVE / DENY"
        # instruction the coordinator matches literally — a drafter could
        # paraphrase that away and break the match.
        req = OutreachRequest(
            category=OutreachCategory("blocker"), topic=text[:100], context=text,
            salience_score=1.0, signal_type="provision_approval", channel="telegram",
        )
        _result, reply = await pipeline.submit_raw_and_wait(
            text, req, timeout_s=float(timeout_s),
        )
        return reply

    if kind == "disk":
        return await coordinate_grow_disk(remote, _ask, disk=disk, add_gib=gib)
    if kind == "memory":
        return await coordinate_grow_memory(remote, _ask, new_mib=mib)
    return {"ok": False, "error": f"invalid kind {kind!r} (disk|memory)"}
