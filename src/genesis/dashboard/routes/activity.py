"""Activity feed and CC session routes."""

from __future__ import annotations

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/activity")
@_async_route
async def activity_feed():
    """Return events — persistent DB first, ring buffer fallback."""
    import contextlib
    import dataclasses

    from genesis.observability.types import Severity, Subsystem
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify([])

    limit = request.args.get("limit", 50, type=int)
    min_severity_str = request.args.get("min_severity")
    subsystem_str = request.args.get("subsystem")
    since = request.args.get("since")
    until = request.args.get("until")
    search = request.args.get("search")

    if rt.db is not None:
        from genesis.db.crud import events as events_crud

        try:
            rows = await events_crud.query(
                rt.db,
                subsystem=subsystem_str,
                severity=min_severity_str,
                since=since,
                until=until,
                search=search,
                limit=min(limit, 500),
            )
            return jsonify(rows)
        except Exception:
            pass

    if rt.event_bus is None:
        return jsonify([])

    kwargs: dict = {"limit": min(limit, 200)}

    if min_severity_str:
        with contextlib.suppress(ValueError):
            kwargs["min_severity"] = Severity(min_severity_str)

    if subsystem_str:
        with contextlib.suppress(ValueError):
            kwargs["subsystem"] = Subsystem(subsystem_str)

    events = rt.event_bus.recent_events(**kwargs)
    return jsonify([dataclasses.asdict(e) for e in events])


@blueprint.route("/api/genesis/sessions")
@_async_route
async def session_history():
    """Return CC session history from the database."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    limit = request.args.get("limit", 50, type=int)
    status = request.args.get("status")
    channel = request.args.get("channel")
    session_type = request.args.get("session_type")
    since = request.args.get("since")

    clauses: list[str] = []
    params: list = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if session_type:
        clauses.append("session_type = ?")
        params.append(session_type)
    if since:
        clauses.append("started_at >= ?")
        params.append(since)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(limit, 200))

    import aiosqlite

    rt.db.row_factory = aiosqlite.Row
    rows = await rt.db.execute_fetchall(
        f"SELECT * FROM cc_sessions{where} ORDER BY started_at DESC LIMIT ?",
        params,
    )
    return jsonify([dict(r) for r in rows])


@blueprint.route("/api/genesis/sessions/<session_id>/events")
@_async_route
async def session_events(session_id: str):
    """Return events for a specific CC session."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    from genesis.db.crud import events as events_crud

    rows = await events_crud.query(
        rt.db,
        session_id=session_id,
        limit=100,
    )
    return jsonify(rows)
