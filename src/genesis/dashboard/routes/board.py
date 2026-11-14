"""Dashboard route for the read-only Board tab (#4510).

One endpoint, no assembly of its own: it returns ``board_status``'s own
function's output unchanged, so the tab and the MCP tool can never disagree
about the board. That function reads only the reconciler's stored heartbeats,
never GitHub, so opening the tab costs no API calls and cannot stall on gh.
"""

from __future__ import annotations

import logging

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/board")
@_async_route
async def board_view():
    """The board as the reconciler last read it.

    The not-bootstrapped case returns an explicit ``unavailable`` rather than
    an empty view: no counts is not zero counts.
    """
    from genesis.mcp.health.board_tools import _impl_board_status
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify(
            {
                "status": "unavailable",
                "reason": "runtime not bootstrapped — the board's state is unknown, not empty",
            }
        )
    return jsonify(await _impl_board_status(rt.db))
