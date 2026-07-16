"""MCP RPC shim: approval-gated hypervisor grow, run in the server process.

The standalone MCP subprocess has no `OutreachPipeline` (it bootstraps with
`pipeline=None`), so `provision_grow` there can't ask the owner for approval.
This route is the bridge: the MCP tool POSTs here, and `@_async_route`
dispatches the handler onto the runtime event loop that owns the live pipeline
and its single-owner Telegram reply-waiter.

Access posture (deliberate, see also `routes/outreach.py`): this is a plain
`/api/*` route, and — like the rest of the dashboard API — it is reachable by
LAN clients via the incus `0.0.0.0:5000 → 127.0.0.1:5000` proxy, which makes
every request present as loopback (so an IP guard would be non-functional). No
extra auth is added here: the operation is intrinsically **owner-approval-gated**
— it sends an APPROVE/DENY prompt to the owner's own channel and only on an
explicit APPROVE runs the host execute verb (which re-checks the guardian
due-diligence gate). A LAN caller can at worst prompt the owner; nothing mutates
without the owner's reply.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/provision/grow", methods=["POST"])
@_async_route
async def provision_grow_rpc():
    """Grow this VM's disk or RAM from the hypervisor — approval-gated.

    Body: {kind: "disk"|"memory", disk, gib, mib, timeout_seconds}. Blocks until
    the owner replies APPROVE/DENY (or the timeout). Returns the coordinator dict.
    """
    from genesis.outreach.rpc import grow_via_pipeline
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.outreach_pipeline is None:
        return jsonify({"ok": False, "error": "outreach pipeline not ready"}), 503

    data = request.get_json(silent=True) or {}
    result = await grow_via_pipeline(
        rt.outreach_pipeline,
        kind=data.get("kind", "disk"),
        disk=data.get("disk", "scsi1"),
        gib=int(data.get("gib", 0)),
        mib=int(data.get("mib", 0)),
        cpu=int(data.get("cpu", 0)),
        timeout_s=float(data.get("timeout_seconds", 1800)),
    )
    return jsonify(result)
