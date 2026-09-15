"""Dashboard route for the zero-drop accounting view.

One endpoint, one assembler. The route does no counting of its own — it calls
``zero_drop_view.build_view``, as the morning report's ground-truth line does,
so the two accounting surfaces cannot disagree about what the board says.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

# How many findings the gaps listing PAGES. The counts beside it are full
# COUNTs, so the panel renders "n of N" and a reader can see the page is a page.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200


@blueprint.route("/api/genesis/zero-drop")
@_async_route
async def zero_drop_view():
    """The five-part accounting view.

    The not-bootstrapped case returns an explicit ``unavailable`` rather than
    an empty view: a board of zeroes is indistinguishable from a clean one, and
    telling those apart is the entire purpose of this surface.
    """
    from genesis.runtime import GenesisRuntime
    from genesis.session_awareness.zero_drop_view import build_view

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify(
            {
                "status": "unavailable",
                "reason": "runtime not bootstrapped — counts are unknown, not zero",
            }
        )

    try:
        limit = int(request.args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    # CLAMPED, not rejected — and that is safe here only because this bounds a
    # PAGE, not a value we store or a total we report. The counts beside the
    # listing are full COUNTs, so a clamped page still renders "n of N" and the
    # denominator stays true whatever the caller asked for.
    limit = max(1, min(limit, _MAX_LIMIT))

    view = await build_view(rt.db, now=datetime.now(UTC), findings_limit=limit)
    return jsonify(view)
