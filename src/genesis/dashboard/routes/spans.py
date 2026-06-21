"""Dashboard routes for the Traces (trace-waterfall) panel.

Read-only consumers of :mod:`genesis.observability.span_reader`. No writes, no
schema changes — they surface the ``otel_spans`` tracing backbone (PR #718) as a
nested waterfall in the dashboard.

``/api/*`` routes are auth-open via the ``before_request`` hook in ``auth.py``
(auth gates the web UI only), so no per-route auth is needed for read-only span
data. Span attributes are already secret-free at capture time (the CC span hook
truncates inputs and never records file contents or env).
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/spans/recent")
@_async_route
async def spans_recent():
    """Most recent traces (root spans), newest first, with span counts."""
    from genesis.observability import span_reader
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"traces": []})

    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    traces = await span_reader.list_recent_traces(rt.db, limit=limit)
    return jsonify({"traces": traces})


@blueprint.route("/api/genesis/spans/trace/<trace_id>")
@_async_route
async def spans_trace(trace_id: str):
    """One trace, flattened depth-first into waterfall render order.

    Returns ``{trace_id, span_count, spans:[...]}`` where each span carries an
    added ``depth`` (0=root), its parsed ``attributes``, and no ``children`` key
    (the tree is pre-flattened server-side via the tested
    :func:`span_reader.flatten_tree`). Unknown trace → 404.
    """
    from genesis.observability import span_reader
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    trace = await span_reader.get_trace(rt.db, trace_id)
    if trace is None:
        return jsonify({"error": "Trace not found"}), 404

    spans = span_reader.flatten_tree(trace)  # sync; adds `depth`, drops `children`
    return jsonify(
        {
            "trace_id": trace["trace_id"],
            "span_count": trace["span_count"],
            "spans": spans,
        }
    )
